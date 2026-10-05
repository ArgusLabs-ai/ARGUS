"""Replay reruns a node off the notebook, not off a side copy of the run file.

The split that matters: the *input* comes from the ledger row (the state that
node really saw, rebuilt from steps that passed and are never re-executed), the
*function* comes from the caller's live graph. The row's old update stays where
it is, as the baseline for what happened last time.
"""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest

from argus.ledger import build_ledger
from argus.recorder import ArgusRecorder
from argus.replay import ReplayEngine
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    query: str
    docs: list
    summary: str
    answer: str


def _app(summarize):
    """search → summarize → answer. `summarize` is swapped per test."""

    def search(s: _S) -> dict:
        return {"docs": [f"doc about {s['query']}"]}

    def answer(s: _S) -> dict:
        return {"answer": s.get("summary", "(nothing)")}

    g = StateGraph(_S)
    g.add_node("search", search)
    g.add_node("summarize", summarize)
    g.add_node("answer", answer)
    g.add_edge(START, "search")
    g.add_edge("search", "summarize")
    g.add_edge("summarize", "answer")
    g.add_edge("answer", END)
    return g.compile()


def _silent(s: _S) -> dict:
    return {}  # the silent no-op we are replaying


def _fixed(s: _S) -> dict:
    return {"summary": f"summary of {len(s['docs'])} docs"}


@pytest.fixture
def broken_run(tmp_path, monkeypatch):
    """One recorder run where `summarize` returned {}."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)
    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(_app(_silent)).invoke({"query": "silent failures"})
    return recorder.session.run_id


def _ledger(run_id: str):
    record = load_run(run_id)
    return build_ledger(record.steps, record.initial_state, record.reducer_kinds)


def _rows(run_id: str):
    return {r.node: r for r in _ledger(run_id)}


# ── no app → refuse, and say what does work ─────────────────────────────────


@pytest.mark.integration
def test_replaying_a_trace_without_an_app_refuses_and_points_at_check(broken_run):
    with pytest.raises(ValueError, match=r"argus check"):
        ReplayEngine().replay_live(broken_run, "summarize")


@pytest.mark.integration
def test_a_node_that_is_not_in_the_notebook_is_named_not_guessed(broken_run):
    with pytest.raises(ValueError, match=r"Node 'typo' not found.*summarize"):
        ReplayEngine().replay_live(broken_run, "typo", app=_app(_fixed))


@pytest.mark.integration
def test_an_app_that_no_longer_has_the_node_says_so(broken_run):
    """The run has `summarize`; the graph the caller passed does not."""
    g = StateGraph(_S)
    g.add_node("search", lambda s: {"docs": []})
    g.add_edge(START, "search")
    g.add_edge("search", END)

    with pytest.raises(ValueError, match="no runnable node 'summarize'"):
        ReplayEngine().replay_live(broken_run, "summarize", app=g.compile())


# ── the function is fed the row, and only the row ───────────────────────────


@pytest.mark.integration
def test_the_function_receives_exactly_the_ledger_row_input(broken_run):
    seen: list[dict] = []

    def spy(s: _S) -> dict:
        seen.append(dict(s))
        return {"summary": "ok"}

    ReplayEngine().replay_live(broken_run, "summarize", app=_app(spy))

    assert seen == [_rows(broken_run)["summarize"].input_state]


@pytest.mark.integration
def test_patching_the_row_input_changes_what_the_function_sees(broken_run):
    """The public patch edits the row replay re-feeds. A side channel would not move."""
    seen: list[dict] = []

    def spy(s: _S) -> dict:
        seen.append(dict(s))
        return {"summary": "ok"}

    ReplayEngine().replay_live(
        broken_run, "summarize", app=_app(spy), patch={"set": {"docs": ["patched"]}}
    )

    assert seen[0]["docs"] == ["patched"]
    assert seen[0]["query"] == _rows(broken_run)["summarize"].input_state["query"]


@pytest.mark.integration
def test_the_input_is_the_replayed_nodes_row_not_its_predecessors_output(broken_run):
    """`search` wrote only `docs`. `summarize` was handed `query` too."""
    row = _rows(broken_run)["summarize"]

    assert row.input_state["query"] == "silent failures"
    assert row.input_state["docs"] == _rows(broken_run)["search"].update["docs"]


# ── the old run is a baseline, not a workspace ──────────────────────────────


@pytest.mark.integration
def test_the_fix_produces_a_new_output_and_leaves_the_old_one_alone(broken_run):
    before = _rows(broken_run)["summarize"]
    assert before.update == {}, "the silent failure we are replaying"

    new_id = ReplayEngine().replay_live(broken_run, "summarize", app=_app(_fixed))

    assert _rows(new_id)["summarize"].update == {"summary": "summary of 1 docs"}
    assert _rows(broken_run)["summarize"].update == {}, "last time's answer stands"
    assert new_id != broken_run


@pytest.mark.integration
def test_upstream_stays_frozen_and_the_original_notebook_is_untouched(broken_run):
    before = [dataclasses.asdict(r) for r in _ledger(broken_run)]

    new_id = ReplayEngine().replay_live(broken_run, "summarize", app=_app(_fixed))

    assert [dataclasses.asdict(r) for r in _ledger(broken_run)] == before
    # `search` passed — it is not re-run, so it is not a row in the new notebook.
    assert [r.node for r in _ledger(new_id)] == ["summarize"]
    assert load_run(new_id).parent_run_id == broken_run


@pytest.mark.integration
def test_replaying_a_node_does_not_continue_into_the_next_one(broken_run):
    """Checking `summarize` stops at `summarize`. `answer` is a separate ask."""
    ran: list[str] = []

    def spy(s: _S) -> dict:
        ran.append("summarize")
        return {"summary": "ok"}

    new_id = ReplayEngine().replay_live(broken_run, "summarize", app=_app(spy))

    assert ran == ["summarize"]
    assert "answer" not in [r.node for r in _ledger(new_id)]
