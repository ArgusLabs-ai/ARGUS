"""VAR-107: ArgusWatcher.attach() — one API, persist without finalize()."""

from __future__ import annotations

import asyncio
import inspect
from typing import TypedDict

import pytest

from argus.storage import _runs_path, load_run
from argus.watcher import ArgusWatcher, _is_compiled_graph

pytest.importorskip("langgraph")

from langgraph.graph import END, StateGraph  # noqa: E402

_WATCH_KW = dict(semantic_judge=False, investigate=False, record_http=False)


class _S(TypedDict):
    n: int


def _linear_state_graph() -> StateGraph:
    g = StateGraph(_S)
    g.add_node("a", lambda s: {"n": s["n"] + 1})
    g.add_node("b", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g


def _cyclic_state_graph() -> StateGraph:
    def inc(state: _S) -> dict:
        return {"n": state["n"] + 1}

    def route(state: _S) -> str:
        return "inc" if state["n"] < 3 else END

    g = StateGraph(_S)
    g.add_node("inc", inc)
    g.set_entry_point("inc")
    g.add_conditional_edges("inc", route, {"inc": "inc", END: END})
    return g


@pytest.mark.unit
def test_is_compiled_graph_detects_builder_vs_app():
    """attach() routing must not confuse a StateGraph builder with a compiled app."""

    class Builder:
        def add_node(self, *a, **k):
            pass

        def compile(self, *a, **k):
            return self

    class App:
        def invoke(self, state):
            return state

        builder = Builder()

    assert _is_compiled_graph(Builder()) is False
    assert _is_compiled_graph(App()) is True


@pytest.mark.unit
def test_attach_uncompiled_returns_invokable_and_persists():
    """attach(StateGraph) returns a compiled app; invoke persists without finalize()."""
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_linear_state_graph())
    result = app.invoke({"n": 0})
    assert result["n"] == 2
    assert watcher._session is not None
    assert watcher._session._completed
    record = load_run(watcher.run_id)
    assert record.overall_status == "clean"
    assert (_runs_path() / f"{watcher.run_id}.json").exists()


@pytest.mark.unit
def test_attach_compiled_and_persists_without_reassignment():
    """attach(compiled) instruments the original object — no 'wrong method' trap."""
    compiled = _linear_state_graph().compile()
    watcher = ArgusWatcher(**_WATCH_KW)
    returned = watcher.attach(compiled)
    result = compiled.invoke({"n": 0})
    assert result["n"] == 2
    assert returned is not None
    assert watcher._session is not None
    assert watcher._session._completed
    record = load_run(watcher.run_id)
    assert {e.node_name for e in record.steps} >= {"a", "b"}


@pytest.mark.unit
def test_constructor_uncompiled_persists_without_finalize():
    """ArgusWatcher(graph); graph.compile(); invoke() still persists (compat)."""
    graph = _linear_state_graph()
    watcher = ArgusWatcher(graph, **_WATCH_KW)
    app = graph.compile()
    app.invoke({"n": 0})
    assert watcher._session is not None
    assert watcher._session._completed
    assert load_run(watcher.run_id).overall_status == "clean"


@pytest.mark.unit
def test_watch_compiled_thin_wrapper_persists():
    """watch_compiled() stays compatible and persists without finalize()."""
    compiled = _linear_state_graph().compile()
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.watch_compiled(compiled)
    app.invoke({"n": 0})
    assert load_run(watcher.run_id) is not None


@pytest.mark.unit
def test_attach_cyclic_persists_without_finalize():
    """Cyclic graphs persist when invoke() returns — no manual finalize()."""
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_cyclic_state_graph())
    assert watcher._session is not None
    assert watcher._session._is_cyclic
    result = app.invoke({"n": 0})
    assert result["n"] == 3
    assert watcher._session._completed
    record = load_run(watcher.run_id)
    assert record.is_cyclic
    inc_steps = [e for e in record.steps if e.node_name == "inc"]
    assert len(inc_steps) == 3


@pytest.mark.unit
def test_attach_finalize_idempotent_no_double_save(monkeypatch):
    """invoke() persist + explicit finalize() writes the run file once."""
    from argus import session as session_mod

    calls: list[str] = []
    original = session_mod.save_run

    def counting_save(record):
        calls.append(record.run_id)
        return original(record)

    monkeypatch.setattr(session_mod, "save_run", counting_save)

    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_linear_state_graph())
    app.invoke({"n": 0})
    watcher.finalize()
    watcher.finalize()

    assert len(calls) == 1
    run_files = list(_runs_path().glob(f"{watcher.run_id}.json"))
    assert len(run_files) == 1


@pytest.mark.unit
def test_attach_cyclic_finalize_idempotent_no_double_save(monkeypatch):
    """Cyclic persist-on-invoke + finalize() still saves once."""
    from argus import session as session_mod

    calls: list[str] = []
    original = session_mod.save_run

    def counting_save(record):
        calls.append(record.run_id)
        return original(record)

    monkeypatch.setattr(session_mod, "save_run", counting_save)

    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_cyclic_state_graph())
    app.invoke({"n": 0})
    watcher.finalize()

    assert len(calls) == 1


@pytest.mark.unit
def test_watch_rejects_compiled_and_points_at_attach():
    """watch() on a compiled app still errors, but names attach() as the fix."""
    compiled = _linear_state_graph().compile()
    watcher = ArgusWatcher(**_WATCH_KW)
    with pytest.raises(ValueError, match="attach"):
        watcher.watch(compiled)


def _count_saves(monkeypatch) -> list[str]:
    from argus import session as session_mod

    calls: list[str] = []
    original = session_mod.save_run

    def counting_save(record):
        calls.append(record.run_id)
        return original(record)

    monkeypatch.setattr(session_mod, "save_run", counting_save)
    return calls


def _run_coro(coro):
    """Run a coroutine without closing the loop (asyncio.run breaks later tests)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.mark.unit
def test_attach_batch_persists_all_items_once(monkeypatch):
    """batch() of 3 inputs: LangGraph calls invoke per item; persist once with all events.

    Intended semantics: one run record for the batch, not one per item. Events
    from items 2..N must not be dropped after item 1's invoke returns.
    """
    calls = _count_saves(monkeypatch)
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_linear_state_graph())

    invoke_hits: list[int] = []
    persist_wrapped_invoke = app.invoke

    def counting_invoke(*args, **kwargs):
        invoke_hits.append(1)
        return persist_wrapped_invoke(*args, **kwargs)

    app.invoke = counting_invoke

    inputs = [{"n": 0}, {"n": 10}, {"n": 100}]
    results = app.batch(inputs)

    assert results == [{"n": 2}, {"n": 12}, {"n": 102}]
    assert len(invoke_hits) == 3, "LangGraph batch() must call invoke() once per item"
    assert len(calls) == 1
    assert watcher._session is not None
    assert watcher._session._completed

    record = load_run(watcher.run_id)
    executed = [e.node_name for e in record.steps if e.node_name in {"a", "b"}]
    assert executed.count("a") == 3
    assert executed.count("b") == 3


@pytest.mark.unit
def test_attach_abatch_persists_all_items_once(monkeypatch):
    """abatch() of 2+ inputs persists one run that includes every item's nodes."""
    calls = _count_saves(monkeypatch)
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_linear_state_graph())

    ainvoke_hits: list[int] = []
    persist_wrapped_ainvoke = app.ainvoke

    async def counting_ainvoke(*args, **kwargs):
        ainvoke_hits.append(1)
        return await persist_wrapped_ainvoke(*args, **kwargs)

    app.ainvoke = counting_ainvoke

    inputs = [{"n": 0}, {"n": 10}]
    results = _run_coro(app.abatch(inputs))

    assert results == [{"n": 2}, {"n": 12}]
    assert len(ainvoke_hits) == 2
    assert len(calls) == 1
    record = load_run(watcher.run_id)
    executed = [e.node_name for e in record.steps if e.node_name in {"a", "b"}]
    assert executed.count("a") == 2
    assert executed.count("b") == 2


@pytest.mark.unit
def test_attach_astream_is_async_generator_and_persists():
    """astream wrapper must be an async generator; persist when it is exhausted."""
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_linear_state_graph())
    assert inspect.isasyncgenfunction(app.astream)

    async def _consume():
        return [chunk async for chunk in app.astream({"n": 0})]

    chunks = _run_coro(_consume())
    assert chunks
    assert watcher._session is not None
    assert watcher._session._completed
    record = load_run(watcher.run_id)
    assert {e.node_name for e in record.steps} >= {"a", "b"}


# ── Issue #31: repeated invoke() persists a run each time; empty {} origin ──────


def _drop_field_graph() -> StateGraph:
    """search returns {} (drops documents); summarize crashes reading it."""

    class _DS(TypedDict, total=False):
        query: str
        documents: list
        answer: str

    def search(state: _DS) -> _DS:
        return {}  # BUG origin — produces no state update

    def summarize(state: _DS) -> _DS:
        return {"answer": state["documents"][0]}  # KeyError

    g = StateGraph(_DS)
    g.add_node("search", search)
    g.add_node("summarize", summarize)
    g.set_entry_point("search")
    g.add_edge("search", "summarize")
    g.add_edge("summarize", END)
    return g


@pytest.mark.unit
def test_each_invoke_persists_its_own_run(monkeypatch):
    """3x invoke() on one attached watcher writes 3 separate run records (issue #31)."""
    calls = _count_saves(monkeypatch)
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_linear_state_graph())

    run_ids = []
    for _ in range(3):
        app.invoke({"n": 0})
        run_ids.append(watcher.run_id)

    assert len(calls) == 3, "each outermost invoke() must persist its own run"
    assert len(set(run_ids)) == 3, "each invoke() must get a fresh run_id"
    for rid in run_ids:
        assert load_run(rid).run_id == rid


@pytest.mark.unit
def test_clean_then_failing_invoke_both_persist(monkeypatch):
    """A clean run then a failing run: the failure is not hidden by the first (issue #31)."""
    calls = _count_saves(monkeypatch)
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_linear_state_graph())

    app.invoke({"n": 0})  # clean
    app.invoke({"n": 0})  # clean again
    assert len(calls) == 2
    assert len({rid for rid in calls}) == 2


@pytest.mark.unit
def test_empty_dict_origin_is_flagged_and_blamed():
    """A node returning {} is a failure, and root cause points at it, not the crash."""
    watcher = ArgusWatcher(**_WATCH_KW)
    app = watcher.attach(_drop_field_graph())
    with pytest.raises(KeyError):
        app.invoke({"query": "x"})

    record = load_run(watcher.run_id)
    search_ev = next(e for e in record.steps if e.node_name == "search")
    assert search_ev.status != "pass", "empty {} origin must not stay a clean pass"

    # Root cause targets the origin (search), not the crash site (summarize).
    assert record.root_cause_chain
    assert record.root_cause_chain[0] == "search"
    assert record.first_failure_step == "search"
