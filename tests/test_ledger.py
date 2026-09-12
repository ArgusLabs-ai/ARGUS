"""Spike 2: the notebook survives the run file — re-score with no live graph.

Invoke once, load the saved run, build the ledger from *that*, and grade it.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

import pytest

from argus.check import evaluate_run
from argus.ledger import build_ledger
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    query: str
    docs: list
    summary: str
    answer: str


@tool
def fetch_docs(q: str) -> list:
    """Look up documents for a query."""
    return [f"doc about {q}"]


def _app():
    def search(state: _S) -> dict:
        return {"docs": fetch_docs.invoke({"q": state["query"]})}

    def summarize(state: _S) -> dict:
        return {}  # the silent no-op

    def answer(state: _S) -> dict:
        return {"answer": state.get("summary", "(nothing)")}

    g = StateGraph(_S)
    g.add_node("search", search)
    g.add_node("summarize", summarize)
    g.add_node("answer", answer)
    g.add_edge(START, "search")
    g.add_edge("search", "summarize")
    g.add_edge("summarize", "answer")
    g.add_edge("answer", END)
    return g.compile()


@pytest.mark.integration
def test_ledger_survives_reload_and_regrades():
    recorder = ArgusRecorder()
    recorder.attach(_app()).invoke({"query": "silent failures"})

    # Everything below reads the file. No second invoke, no live graph.
    loaded = load_run(recorder.session.run_id)
    rows = {r.node: r for r in build_ledger(loaded.steps, loaded.initial_state)}

    silent = rows["summarize"]
    assert silent.update == {}, "the step's own return value was merged away"
    assert silent.state_after["docs"], "running state must still carry what search wrote"

    tools = rows["search"].tools
    assert len(tools) == 1
    assert tools[0]["name"] == "fetch_docs"
    assert tools[0]["output"] == ["doc about silent failures"]
    assert tools[0]["error"] is None

    assert rows["summarize"].tools == []
    assert not evaluate_run(loaded).passed
    assert "summarize" in evaluate_run(loaded).failing_nodes


class _C(TypedDict, total=False):
    route: str
    left: str
    right: str
    out: str


def _branching_app():
    """A conditional edge, so one node never runs.

    The linear graph above cannot catch a divergence between the live notebook
    and the reloaded one: the `skipped` events that used to cause it are
    synthesized at finalize, and only a graph with an untaken branch has any.
    """
    g = StateGraph(_C)
    g.add_node("router", lambda s: {"route": "left"})
    g.add_node("left", lambda s: {"left": "L"})
    g.add_node("right", lambda s: {"right": "R"})
    g.add_node("sink", lambda s: {"out": s.get("left") or s.get("right")})
    g.add_edge(START, "router")
    g.add_conditional_edges("router", lambda s: s["route"], {"left": "left", "right": "right"})
    g.add_edge("left", "sink")
    g.add_edge("right", "sink")
    g.add_edge("sink", END)
    return g.compile()


@pytest.mark.integration
def test_an_untaken_branch_is_not_in_the_notebook():
    recorder = ArgusRecorder()
    recorder.attach(_branching_app()).invoke({})

    loaded = load_run(recorder.session.run_id)
    assert any(s.node_name == "right" and s.status == "skipped" for s in loaded.steps)
    rows = build_ledger(loaded.steps, loaded.initial_state)
    assert "right" not in [r.node for r in rows]


@pytest.mark.integration
@pytest.mark.parametrize("app", [_app, _branching_app], ids=["linear", "branching"])
def test_ledger_from_live_steps_matches_the_reloaded_one(app):
    """Same notebook either way — that is what makes re-score trustworthy."""
    recorder = ArgusRecorder()
    recorder.attach(app()).invoke({"query": "q"})
    session = recorder.session

    loaded = load_run(session.run_id)
    live = build_ledger(session._events, session._initial_state)
    reloaded = build_ledger(loaded.steps, loaded.initial_state)

    assert [(r.node, r.update, r.state_after, r.tools) for r in live] == [
        (r.node, r.update, r.state_after, r.tools) for r in reloaded
    ]


class _F(TypedDict, total=False):
    docs: Annotated[list, operator.add]
    report: str


def _fan_in_app():
    """Two branches accumulating into one `Annotated[list, operator.add]` field."""
    g = StateGraph(_F)
    g.add_node("left", lambda s: {"docs": ["from-left"]})
    g.add_node("right", lambda s: {"docs": ["from-right"]})
    g.add_node("join", lambda s: {"report": f"{len(s['docs'])} docs"})
    g.add_edge(START, "left")
    g.add_edge(START, "right")
    g.add_edge("left", "join")
    g.add_edge("right", "join")
    g.add_edge("join", END)
    return g.compile()


@pytest.mark.integration
def test_a_reduced_field_accumulates_in_the_running_state():
    """A plain overlay makes fan-in look like the last branch overwrote the first.

    The notebook's running state has to agree with what the next node was really
    handed, or the backward walk that decides who dropped a field reads a state
    that never existed.
    """
    recorder = ArgusRecorder()
    final = recorder.attach(_fan_in_app()).invoke({"docs": []})

    loaded = load_run(recorder.session.run_id)
    rows = build_ledger(loaded.steps, loaded.initial_state, loaded.reducer_kinds)
    by_node = {r.node: r for r in rows}

    assert final["docs"] == ["from-left", "from-right"]
    # What `join` was really handed, straight off the trace.
    assert sorted(by_node["join"].input_state["docs"]) == ["from-left", "from-right"]
    # ...and what the notebook says the state was when `join` ran.
    assert sorted(by_node["join"].state_after["docs"]) == ["from-left", "from-right"]


@pytest.mark.integration
def test_reduced_fan_in_reads_the_same_live_and_reloaded():
    recorder = ArgusRecorder()
    recorder.attach(_fan_in_app()).invoke({"docs": []})
    session = recorder.session

    loaded = load_run(session.run_id)
    live = build_ledger(session._events, session._initial_state, session.reducer_kinds)
    reloaded = build_ledger(loaded.steps, loaded.initial_state, loaded.reducer_kinds)

    assert [r.state_after for r in live] == [r.state_after for r in reloaded]


@pytest.mark.unit
def test_an_unknown_reducer_falls_back_to_overwrite():
    """Custom reducer callables do not survive the run file — say so, don't guess."""
    from argus.models import NodeEvent

    def _ev(i, node, update):
        return NodeEvent(
            step_index=i,
            node_name=node,
            status="pass",
            input_state={},
            output_dict=update,
            duration_ms=1.0,
            timestamp_utc="2026-01-01T00:00:00Z",
        )

    steps = [_ev(0, "a", {"docs": ["x"]}), _ev(1, "b", {"docs": ["y"]})]
    assert build_ledger(steps, {}, {"docs": "overwrite"})[-1].state_after["docs"] == ["y"]
    assert build_ledger(steps, {}, {"docs": "add"})[-1].state_after["docs"] == ["x", "y"]
    assert build_ledger(steps, {})[-1].state_after["docs"] == ["y"]
