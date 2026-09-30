"""VAR-63: auto-instrument LangGraph runtime during pytest --argus."""

from __future__ import annotations

import asyncio
from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph

from argus.pytest_instrument import (
    install_auto_instrumentation,
    uninstall_auto_instrumentation,
)
from argus.storage import last_run_id, load_run

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
def test_uninstall_restores_uninstrumented_compile(auto_wrap):
    uninstall_auto_instrumentation()
    app = _clean_graph().compile()
    before = last_run_id()
    app.invoke({"n": 0})
    assert last_run_id() == before


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
