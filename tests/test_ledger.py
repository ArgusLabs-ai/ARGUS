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


@pytest.mark.unit
def test_ledger_from_live_steps_matches_the_reloaded_one():
    """Same notebook either way — that is what makes re-score trustworthy."""
    recorder = ArgusRecorder()
    recorder.attach(_app()).invoke({"query": "q"})
    session = recorder.session

    loaded = load_run(session.run_id)
    live = build_ledger(session._events, session._initial_state)
    reloaded = build_ledger(loaded.steps, loaded.initial_state)

    assert [(r.node, r.update, r.state_after, r.tools) for r in live] == [
        (r.node, r.update, r.state_after, r.tools) for r in reloaded
    ]


class _Reduced(TypedDict, total=False):
    docs: Annotated[list, operator.add]


def _reduced_app():
    def seed(state: _Reduced) -> dict:
        return {"docs": ["seed"]}

    def empty(state: _Reduced) -> dict:
        return {"docs": []}

    def tail(state: _Reduced) -> dict:
        return {}

    g = StateGraph(_Reduced)
    g.add_node("a", seed)
    g.add_node("b", empty)
    g.add_node("c", tail)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g.compile()


@pytest.mark.integration
def test_state_after_honours_operator_add_reducer():
    """state_after for a reduced field must match the graph, not last-write-wins."""
    recorder = ArgusRecorder()
    result = recorder.attach(_reduced_app()).invoke({})
    assert result.get("docs") == ["seed"]

    loaded = load_run(recorder.session.run_id)
    rows = {r.node: r for r in build_ledger(loaded.steps, loaded.initial_state)}

    assert rows["b"].update == {"docs": []}
    assert rows["b"].state_after["docs"] == ["seed"]

    live = build_ledger(recorder.session._events, recorder.session._initial_state)
    reloaded = build_ledger(loaded.steps, loaded.initial_state)
    assert [r.state_after for r in live] == [r.state_after for r in reloaded]
