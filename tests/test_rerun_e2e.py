"""The whole loop on one real pipeline: run → detect → blame → rerun.

  ingest → retrieve → rerank → summarize → answer

`rerank`'s score threshold is too strict for the corpus, so it drops every
document and returns ``{"docs": []}``. Nothing raises, the graph completes, and
`answer` produces a fluent sentence built on no sources — the failure this
product exists to catch.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.ledger import build_ledger
from argus.recorder import ArgusRecorder
from argus.replay import ReplayEngine
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

_CORPUS = [
    ("Silent failures are pipeline steps that fail without raising.", 0.81),
    ("A node returning an empty update looks identical to a clean pass.", 0.74),
    ("Retrieval that returns nothing still produces a fluent answer.", 0.66),
]


class _State(TypedDict, total=False):
    question: str
    query: str
    docs: list
    summary: str
    answer: str


@tool
def search_docs(query: str) -> list:
    """Look up documents matching a query."""
    return [{"text": t, "score": s} for t, s in _CORPUS]


def _rerank(threshold: float):
    def rerank(state: _State) -> dict:
        return {"docs": [d for d in state["docs"] if d["score"] > threshold]}

    return rerank


def _build(rerank):
    def ingest(state: _State) -> dict:
        return {"query": state["question"].strip().lower()}

    def retrieve(state: _State) -> dict:
        return {"docs": search_docs.invoke({"query": state["query"]})}

    def summarize(state: _State) -> dict:
        return {"summary": " ".join(d["text"] for d in state.get("docs") or [])}

    def answer(state: _State) -> dict:
        return {"answer": f"Based on the sources: {state.get('summary') or ''}"}

    g = StateGraph(_State)
    for name, fn in (
        ("ingest", ingest),
        ("retrieve", retrieve),
        ("rerank", rerank),
        ("summarize", summarize),
        ("answer", answer),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "retrieve")
    g.add_edge("retrieve", "rerank")
    g.add_edge("rerank", "summarize")
    g.add_edge("summarize", "answer")
    g.add_edge("answer", END)
    return g.compile()


@pytest.fixture
def broken(tmp_path, monkeypatch):
    """The run nobody would notice: no exception, an empty answer."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)

    recorder = ArgusRecorder(semantic_judge=False, consumers={"docs": ["summarize"]})
    app = recorder.attach(_build(_rerank(0.9)))
    final = app.invoke({"question": "  What is a Silent Failure? "})

    assert final["answer"] == "Based on the sources: ", "the graph completed and said nothing"
    return recorder.session.run_id


def _rows(run_id: str) -> dict:
    rec = load_run(run_id)
    return {r.node: r for r in build_ledger(rec.steps, rec.initial_state, rec.reducer_kinds)}


@pytest.mark.integration
def test_the_silent_failure_is_caught_and_blamed_on_its_origin(broken):
    record = load_run(broken)
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert record.overall_status == "silent_failure"
    assert "rerank" in verdict.failing_nodes, "the node that dropped the documents"
    assert "retrieve" not in verdict.failing_nodes, "retrieve did its job"
    assert "answer" not in verdict.failing_nodes, "the last node is the victim, not the cause"

    origin = [f for f in record.findings if f.node == "rerank" and f.severity == "critical"]
    assert origin, "rerank is named in the findings"


@pytest.mark.integration
def test_the_notebook_holds_what_each_step_really_did(broken):
    rows = _rows(broken)

    assert [t["name"] for t in rows["retrieve"].tools] == ["search_docs"]
    assert len(rows["retrieve"].update["docs"]) == 3
    assert rows["rerank"].update["docs"] == [], "the drop, recorded as the node's own return"
    assert len(rows["rerank"].input_state["docs"]) == 3, "…and what it was handed before it"
    assert rows["summarize"].input_state["docs"] == [], "the victim's view"


@pytest.mark.integration
def test_rerunning_the_blamed_node_re_feeds_its_own_row(broken):
    """Not the next node's input, not the final state — the row for `rerank`."""
    rows = _rows(broken)
    seen: list[dict] = []

    def spy(state: _State) -> dict:
        seen.append(dict(state))
        return _rerank(0.6)(state)

    new_id = ReplayEngine().replay_live(broken, "rerank", app=_build(spy))

    assert seen[0] == rows["rerank"].input_state
    assert seen[0] != rows["summarize"].input_state, "the row, not the node after it"
    assert len(seen[0]["docs"]) == 3, "it was handed the documents retrieve found"

    assert len(_rows(new_id)["rerank"].update["docs"]) == 3, "the fix kept them"
    assert evaluate_run(load_run(new_id)).passed is True


@pytest.mark.integration
def test_the_rerun_writes_a_new_run_and_freezes_everything_upstream(broken):
    before = {n: (r.input_state, r.update) for n, r in _rows(broken).items()}

    new_id = ReplayEngine().replay_live(broken, "rerank", app=_build(_rerank(0.6)))

    after = {n: (r.input_state, r.update) for n, r in _rows(broken).items()}
    assert after == before, "the original notebook is a baseline, not a workspace"
    assert _rows(broken)["rerank"].update["docs"] == [], "last time's answer still stands"

    assert list(_rows(new_id)) == ["rerank"], "upstream passed; it is not re-run"
    assert load_run(new_id).parent_run_id == broken


@pytest.mark.integration
def test_without_the_app_the_rerun_refuses_and_names_the_alternative(broken):
    with pytest.raises(ValueError, match=r"argus check"):
        ReplayEngine().replay_live(broken, "rerank")
