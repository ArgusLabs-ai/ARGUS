"""VAR-63: auto-instrument LangGraph runtime during pytest --argus."""

from __future__ import annotations

import asyncio
import threading
from typing import TypedDict

import pytest
from conftest import make_run_record
from langgraph.graph import END, StateGraph

from argus.pytest_instrument import (
    install_auto_instrumentation,
    uninstall_auto_instrumentation,
)
from argus.storage import last_run_id, load_run, save_run

pytest.importorskip("langgraph")

pytest_plugins = ["pytester"]


@pytest.fixture
def auto_wrap():
    install_auto_instrumentation()
    try:
        yield
    finally:
        uninstall_auto_instrumentation()


class _Count(TypedDict):
    n: int


class _ToolState(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int


class _DocState(TypedDict):
    query: str
    documents: list


def _clean_graph() -> StateGraph:
    g = StateGraph(_Count)
    g.add_node("inc", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("inc")
    g.add_edge("inc", END)
    return g


def _silent_graph() -> StateGraph:
    def api_call(state: _ToolState) -> dict:
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state: _ToolState) -> dict:
        return {"n": 1}

    g = StateGraph(_ToolState)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    return g


def _missing_fields_graph() -> StateGraph:
    def retrieve(state: _DocState) -> dict:
        return {"query": state["query"]}

    def answer(state: _DocState) -> dict:
        docs = state.get("documents", [])
        return {"documents": docs}

    g = StateGraph(_DocState)
    g.add_node("retrieve", retrieve)
    g.add_node("answer", answer)
    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer", END)
    return g


@pytest.mark.unit
def test_auto_wrap_clean_invoke_is_clean(auto_wrap):
    app = _clean_graph().compile()
    assert app.invoke({"n": 0})["n"] == 1
    run_id = last_run_id()
    assert run_id is not None
    record = load_run(run_id)
    assert record.overall_status == "clean"


@pytest.mark.unit
def test_auto_wrap_silent_failure_is_recorded(auto_wrap):
    app = _silent_graph().compile()
    app.invoke({"n": 0})
    run_id = last_run_id()
    assert run_id is not None
    record = load_run(run_id)
    assert record.overall_status == "silent_failure"
    api = next(s for s in record.steps if s.node_name == "api_call")
    assert api.inspection is not None
    assert api.inspection.has_tool_failure


def _assert_silent_failure_recorded() -> None:
    run_id = last_run_id()
    assert run_id is not None
    record = load_run(run_id)
    assert record.overall_status == "silent_failure"
    api = next(s for s in record.steps if s.node_name == "api_call")
    assert api.inspection is not None
    assert api.inspection.has_tool_failure


@pytest.mark.unit
def test_auto_wrap_silent_failure_ainvoke(auto_wrap):
    app = _silent_graph().compile()
    asyncio.run(app.ainvoke({"n": 0}))
    _assert_silent_failure_recorded()


@pytest.mark.unit
def test_auto_wrap_silent_failure_stream(auto_wrap):
    app = _silent_graph().compile()
    list(app.stream({"n": 0}))
    _assert_silent_failure_recorded()


@pytest.mark.unit
def test_auto_wrap_silent_failure_astream(auto_wrap):
    app = _silent_graph().compile()

    async def _drain() -> None:
        async for _ in app.astream({"n": 0}):
            pass

    asyncio.run(_drain())
    _assert_silent_failure_recorded()


@pytest.mark.unit
def test_auto_wrap_silent_failure_batch(auto_wrap):
    app = _silent_graph().compile()
    app.batch([{"n": 0}])
    _assert_silent_failure_recorded()


@pytest.mark.unit
def test_auto_wrap_silent_failure_abatch(auto_wrap):
    app = _silent_graph().compile()
    asyncio.run(app.abatch([{"n": 0}]))
    _assert_silent_failure_recorded()


@pytest.mark.unit
def test_auto_wrap_clean_ainvoke_is_clean(auto_wrap):
    app = _clean_graph().compile()
    assert asyncio.run(app.ainvoke({"n": 0}))["n"] == 1
    run_id = last_run_id()
    assert run_id is not None
    record = load_run(run_id)
    assert record.overall_status == "clean"


@pytest.mark.unit
def test_auto_wrap_missing_fields_are_recorded(auto_wrap):
    app = _missing_fields_graph().compile()
    app.invoke({"query": "what is argus?", "documents": []})
    run_id = last_run_id()
    assert run_id is not None
    record = load_run(run_id)
    retrieve = next(s for s in record.steps if s.node_name == "retrieve")
    assert retrieve.inspection is not None
    flagged = (
        retrieve.inspection.is_silent_failure
        or bool(retrieve.inspection.missing_fields)
        or record.overall_status != "clean"
    )
    assert flagged, (
        f"expected missing-field / silent failure, got status={record.overall_status} "
        f"inspection={retrieve.inspection}"
    )


@pytest.mark.unit
def test_reinstall_then_uninstall_leaves_nothing_patched():
    """install → uninstall → install → uninstall must fully restore LangGraph.

    The second install found the methods already wrapped and returned without
    recording the originals, which uninstall had just cleared — so the class
    stayed patched in the host interpreter for good.
    """
    from langgraph.graph.state import StateGraph
    from langgraph.pregel import Pregel

    from argus.watcher import _RUNTIME_METHODS

    for _ in range(2):
        install_auto_instrumentation()
        uninstall_auto_instrumentation()

    still_patched = [
        name
        for name in (*_RUNTIME_METHODS, "compile")
        for cls in (StateGraph if name == "compile" else Pregel,)
        if getattr(getattr(cls, name), "_argus_pytest_wrapped", False)
    ]
    assert not still_patched


@pytest.mark.unit
def test_uninstall_restores_uninstrumented_compile(auto_wrap):
    uninstall_auto_instrumentation()
    app = _clean_graph().compile()
    before = last_run_id()
    app.invoke({"n": 0})
    assert last_run_id() == before


@pytest.mark.unit
def test_run_capture_collects_runs_saved_by_worker_threads():
    from argus.run_context import begin_run_capture, captured_run_ids, end_run_capture

    capture = begin_run_capture()
    try:
        worker = threading.Thread(
            target=lambda: save_run(make_run_record(run_id="thread-run")),
        )
        worker.start()
        worker.join()
        assert captured_run_ids(capture) == {"thread-run"}
    finally:
        end_run_capture(capture)


@pytest.mark.unit
def test_pregel_fallback_attaches_on_ainvoke():
    """Compile before install so only the Pregel class patch can attach."""
    uninstall_auto_instrumentation()
    app = _silent_graph().compile()
    install_auto_instrumentation()
    try:
        asyncio.run(app.ainvoke({"n": 0}))
        _assert_silent_failure_recorded()
    finally:
        uninstall_auto_instrumentation()


@pytest.mark.unit
def test_pregel_fallback_attaches_on_stream():
    uninstall_auto_instrumentation()
    app = _silent_graph().compile()
    install_auto_instrumentation()
    try:
        list(app.stream({"n": 0}))
        _assert_silent_failure_recorded()
    finally:
        uninstall_auto_instrumentation()


@pytest.mark.unit
def test_pregel_fallback_attaches_on_astream():
    uninstall_auto_instrumentation()
    app = _silent_graph().compile()
    install_auto_instrumentation()
    try:

        async def _drain() -> None:
            async for _ in app.astream({"n": 0}):
                pass

        asyncio.run(_drain())
        _assert_silent_failure_recorded()
    finally:
        uninstall_auto_instrumentation()


@pytest.mark.unit
def test_pregel_fallback_attaches_on_batch():
    uninstall_auto_instrumentation()
    app = _silent_graph().compile()
    install_auto_instrumentation()
    try:
        app.batch([{"n": 0}])
        _assert_silent_failure_recorded()
    finally:
        uninstall_auto_instrumentation()


@pytest.mark.unit
def test_pregel_fallback_attaches_on_abatch():
    uninstall_auto_instrumentation()
    app = _silent_graph().compile()
    install_auto_instrumentation()
    try:
        asyncio.run(app.abatch([{"n": 0}]))
        _assert_silent_failure_recorded()
    finally:
        uninstall_auto_instrumentation()


def _prepare_plugin_project(pytester: pytest.Pytester) -> None:
    pytester.makefile(".toml", pyproject="[project]\nname = 'demo'\n")
    (pytester.path / ".argus" / "runs").mkdir(parents=True, exist_ok=True)


_CLEAN_NO_WATCHER = """
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict):
    n: int

def test_clean():
    g = StateGraph(S)
    g.add_node("inc", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("inc")
    g.add_edge("inc", END)
    assert g.compile().invoke({"n": 0})["n"] == 1
"""

_SILENT_NO_WATCHER = """
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent():
    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    g.compile().invoke({"n": 0})
"""

_SILENT_AINVOKE = """
import asyncio
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent():
    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    asyncio.run(g.compile().ainvoke({"n": 0}))
"""

_SILENT_STREAM = """
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent():
    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    list(g.compile().stream({"n": 0}))
"""

_SILENT_ASTREAM = """
import asyncio
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent():
    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    app = g.compile()

    async def _drain():
        async for _ in app.astream({"n": 0}):
            pass

    asyncio.run(_drain())
"""

_PARALLEL_CLEAN = """
import time
from pathlib import Path
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict):
    n: int

def test_clean():
    Path("clean-started").write_text("ready")
    time.sleep(0.5)
    g = StateGraph(S)
    g.add_node("inc", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("inc")
    g.add_edge("inc", END)
    assert g.compile().invoke({"n": 0})["n"] == 1
"""

_PARALLEL_SILENT = """
import time
from pathlib import Path
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent():
    deadline = time.monotonic() + 5
    while not Path("clean-started").exists():
        if time.monotonic() >= deadline:
            raise AssertionError("clean test did not start")
        time.sleep(0.01)

    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    g.compile().invoke({"n": 0})
"""


_SILENT_BATCH = """
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent():
    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    g.compile().batch([{"n": 0}])
"""


_SILENT_ABATCH = """
import asyncio
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent():
    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    asyncio.run(g.compile().abatch([{"n": 0}]))
"""


@pytest.mark.unit
def test_pytest_argus_auto_wraps_clean_invoke(pytester: pytest.Pytester):
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_CLEAN_NO_WATCHER)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(passed=1)


@pytest.mark.unit
def test_pytest_argus_auto_wraps_silent_failure_and_fails_test(pytester: pytest.Pytester):
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_NO_WATCHER)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(failed=1)
    combined = str(result.stdout) + str(result.stderr)
    assert "argus check failed" in combined


@pytest.mark.unit
def test_pytest_argus_silent_ainvoke_fails_test(pytester: pytest.Pytester):
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_AINVOKE)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(failed=1)
    combined = str(result.stdout) + str(result.stderr)
    assert "argus check failed" in combined


@pytest.mark.unit
def test_pytest_argus_silent_stream_fails_test(pytester: pytest.Pytester):
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_STREAM)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(failed=1)
    combined = str(result.stdout) + str(result.stderr)
    assert "argus check failed" in combined


@pytest.mark.unit
def test_pytest_argus_silent_astream_fails_test(pytester: pytest.Pytester):
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_ASTREAM)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(failed=1)
    combined = str(result.stdout) + str(result.stderr)
    assert "argus check failed" in combined


@pytest.mark.unit
def test_pytest_argus_binds_parallel_runs_to_their_own_tests(pytester: pytest.Pytester):
    pytest.importorskip("xdist")
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(test_clean=_PARALLEL_CLEAN, test_silent=_PARALLEL_SILENT)
    result = pytester.runpytest("--argus", "-n", "2", "--dist=loadfile", "-q")
    result.assert_outcomes(passed=1, failed=1)


# ── #78: a configure hook, not a patch ───────────────────────────────────────


@pytest.mark.unit
def test_install_replaces_nothing_on_langgraph():
    from langgraph.graph.state import StateGraph
    from langgraph.pregel import Pregel

    names = ("invoke", "ainvoke", "stream", "astream", "batch", "abatch")
    before = {n: getattr(Pregel, n) for n in names} | {"compile": StateGraph.compile}
    install_auto_instrumentation()
    try:
        after = {n: getattr(Pregel, n) for n in names} | {"compile": StateGraph.compile}
        assert after == before
    finally:
        uninstall_auto_instrumentation()


@pytest.mark.unit
def test_an_explicit_attach_is_not_recorded_twice(auto_wrap):
    from argus import ArgusRecorder
    from argus.storage import list_runs

    before = {r["run_id"] for r in list_runs()}
    ArgusRecorder(semantic_judge=False).attach(_silent_graph().compile()).invoke({"n": 0})
    new = {r["run_id"] for r in list_runs()} - before
    assert len(new) == 1


@pytest.mark.unit
def test_a_graph_invoked_from_a_plain_thread_is_recorded(auto_wrap):
    """Threads a test starts itself do not inherit contextvars; the hook still sees them."""
    app = _silent_graph().compile()
    worker = threading.Thread(target=lambda: app.invoke({"n": 0}))
    worker.start()
    worker.join()
    _assert_silent_failure_recorded()


@pytest.mark.unit
def test_a_graph_composed_under_another_runnable_is_recorded(auto_wrap):
    from langchain_core.runnables import RunnableLambda

    app = _silent_graph().compile()
    (RunnableLambda(lambda x: x) | app).invoke({"n": 0})
    _assert_silent_failure_recorded()


@pytest.mark.unit
def test_a_plain_chain_is_ignored(auto_wrap):
    from langchain_core.runnables import RunnableLambda

    before = last_run_id()
    RunnableLambda(lambda x: x + 1).invoke(1)
    assert last_run_id() == before


@pytest.mark.unit
def test_pytest_argus_silent_batch_fails_test(pytester: pytest.Pytester):
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_BATCH)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(failed=1)
    combined = str(result.stdout) + str(result.stderr)
    assert "argus check failed" in combined


@pytest.mark.unit
def test_pytest_argus_silent_abatch_fails_test(pytester: pytest.Pytester):
    pytest.importorskip("argus.pytest_plugin")
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_ABATCH)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(failed=1)
    combined = str(result.stdout) + str(result.stderr)
    assert "argus check failed" in combined
