"""Spike 4: the judge is a last look, and `argus check` is the answer.

The judge runs after contextual, structure/tools and the signature rules — and
it cannot wash out what they found. No network: the judge is stubbed.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.models import SemanticCheckResult
from argus.recorder import ArgusRecorder
from argus.semantic_checker import _history_lines, _tracked_fields
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    query: str
    docs: list
    summary: str
    answer: str


def _no_patching(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the recorder must not wrap the engine")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _stub_judge(monkeypatch, *, passed: bool, confidence: float = 1.0):
    """Replace the LLM judge. Returns the list of node names it was asked about."""
    asked: list[str] = []

    def _fake(*, node_name, **kwargs):
        asked.append(node_name)
        return (
            SemanticCheckResult(
                passed=passed,
                reason="stubbed verdict",
                confidence=confidence,
                model="stub",
                prompt_tokens=0,
                completion_tokens=0,
                duration_ms=0.0,
            ),
            [],
        )

    monkeypatch.setattr("argus.semantic_checker.check_semantic_coherence", _fake)
    return asked


def _run(monkeypatch, *, summarize_returns: dict, **recorder_kw):
    def search(state: _S) -> dict:
        return {"docs": ["doc-1"]}

    def summarize(state: _S) -> dict:
        return dict(summarize_returns)

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

    recorder = ArgusRecorder(**recorder_kw)
    recorder.attach(g.compile()).invoke({"query": "q"})
    return load_run(recorder.session.run_id)


def _filter_run(monkeypatch, *, judge_passes: bool, **recorder_kw):
    """search writes docs → clean filters them all away → summarize needs them.

    The shape #85 is about: every step looks locally reasonable, and the failure
    only exists in the history of one field.
    """
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return (
            SemanticCheckResult(
                passed=judge_passes,
                reason="stubbed verdict",
                confidence=1.0,
                model="stub",
                prompt_tokens=0,
                completion_tokens=0,
                duration_ms=0.0,
            ),
            [],
        )

    monkeypatch.setattr("argus.semantic_checker.check_semantic_coherence", _fake)

    def search(state: _S) -> dict:
        return {"docs": ["doc-1", "doc-2"]}

    def clean(state: _S) -> dict:
        return {"docs": []}  # a filter that matched nothing — locally fine

    def summarize(state: _S) -> dict:
        return {"summary": f"summary of {len(state.get('docs', []))} docs"}

    g = StateGraph(_S)
    for name, fn in (("search", search), ("clean", clean), ("summarize", summarize)):
        g.add_node(name, fn)
    g.add_edge(START, "search")
    g.add_edge("search", "clean")
    g.add_edge("clean", "summarize")
    g.add_edge("summarize", END)

    recorder = ArgusRecorder(
        semantic_judge=True,
        consumers={"docs": ["summarize"]},
        **recorder_kw,
    )
    recorder.attach(g.compile()).invoke({"query": "q"})
    return load_run(recorder.session.run_id), calls


@pytest.mark.integration
def test_the_judge_is_shown_the_step_that_emptied_the_field(monkeypatch):
    """#85: judging `summarize` alone is blind — it must see `clean` empty docs."""
    _no_patching(monkeypatch)
    record, calls = _filter_run(monkeypatch, judge_passes=True)

    asked = {c["node_name"]: c for c in calls}
    assert "summarize" in asked, "the judge must have been asked about the reader"

    rows = asked["summarize"]["prior_rows"]
    assert [r.node for r in rows] == ["search", "clean"], "only the steps before it"

    # And the prompt actually carries that history, scoped to `docs`.
    history = _history_lines(
        rows,
        _tracked_fields("summarize", asked["summarize"]["consumers"], {}, {}),
    )
    assert any('"search"' in line and "populated" in line for line in history)
    assert any('"clean"' in line and "now empty" in line for line in history)

    # Both layers land on the same origin: the rules blame `clean`, and the
    # judge is now looking at the row that says `clean` is where docs went.
    verdict = evaluate_run(record)
    assert verdict.passed is False
    assert "clean" in verdict.failing_nodes
    assert "summarize" not in verdict.failing_nodes, "the reader is the victim"


@pytest.mark.integration
def test_history_does_not_let_a_judge_pass_clear_the_dropper(monkeypatch):
    """The judge sees more, and still cannot flip a contextual critical to pass."""
    _no_patching(monkeypatch)
    record, _ = _filter_run(monkeypatch, judge_passes=True)

    missing = [f for f in record.findings if f.type == "missing_field"]
    assert missing and missing[0].node == "clean"
    assert evaluate_run(record).passed is False


@pytest.mark.integration
def test_history_is_scoped_to_the_fields_the_node_touches(monkeypatch):
    """Trace size must not grow with the run — unrelated fields stay out."""
    rows = [
        type("R", (), {"node": "a", "update": {"docs": ["x"], "trace_id": "abc"}})(),
        type("R", (), {"node": "b", "update": {"trace_id": "def"}})(),
    ]
    fields = _tracked_fields("summarize", {"docs": ["summarize"]}, {}, {"summary": ""})
    lines = _history_lines(rows, fields)

    assert len(lines) == 1 and "docs" in lines[0]
    assert all("trace_id" not in line for line in lines)


@pytest.mark.integration
def test_a_confident_judge_pass_cannot_wash_out_an_empty_update(monkeypatch):
    """The one that matters: rules ran first, and the judge does not get to undo them."""
    _no_patching(monkeypatch)
    asked = _stub_judge(monkeypatch, passed=True, confidence=1.0)

    record = _run(monkeypatch, summarize_returns={}, semantic_judge=True)
    verdict = evaluate_run(record)

    # It must have been asked about summarize specifically, or the override
    # path was never exercised and this test would pass by accident.
    assert "summarize" in asked
    assert verdict.passed is False
    assert "summarize" in verdict.failing_nodes

    empty = [f for f in record.findings if f.type == "empty_output"]
    assert empty and empty[0].node == "summarize"


@pytest.mark.integration
def test_rules_fail_the_gate_with_the_judge_off(monkeypatch):
    """Default recorder: no judge, no key, no network — the gate still fails."""
    _no_patching(monkeypatch)
    asked = _stub_judge(monkeypatch, passed=True)

    # No key configured (conftest forces is_available=False), so the default
    # (semantic_judge=None) resolves to off.
    record = _run(monkeypatch, summarize_returns={})

    assert asked == [], "the judge must not run when no key is available"
    assert evaluate_run(record).passed is False
    assert all(step.semantic_check is None for step in record.steps)


@pytest.mark.integration
def test_judge_defaults_on_when_a_key_is_available(monkeypatch):
    """A configured key is intent enough — no second opt-in flag needed."""
    _no_patching(monkeypatch)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    asked = _stub_judge(monkeypatch, passed=True, confidence=1.0)

    # semantic_judge left at its default (None → auto).
    record = _run(monkeypatch, summarize_returns={"summary": "a real summary of doc-1"})

    assert "summarize" in asked, "the judge should run automatically once a key exists"
    assert record.overall_status == "clean"


@pytest.mark.integration
def test_explicit_false_keeps_the_judge_off_even_with_a_key(monkeypatch):
    """The user can always opt back out."""
    _no_patching(monkeypatch)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    asked = _stub_judge(monkeypatch, passed=True)

    record = _run(monkeypatch, summarize_returns={"summary": "fine"}, semantic_judge=False)

    assert asked == [], "explicit False must win over a present key"
    assert all(step.semantic_check is None for step in record.steps)


@pytest.mark.integration
def test_the_judge_can_still_fail_a_step_the_rules_cleared(monkeypatch):
    """Fluent but wrong: no rule fired, the last look did."""
    _no_patching(monkeypatch)
    _stub_judge(monkeypatch, passed=False, confidence=0.9)

    record = _run(
        monkeypatch,
        summarize_returns={"summary": "a fluent summary of the wrong thing"},
        semantic_judge=True,
    )
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert record.overall_status == "silent_failure"
    assert any(step.status == "semantic_fail" for step in record.steps)


@pytest.mark.integration
def test_a_confident_judge_pass_cannot_wash_out_a_contextual_miss(monkeypatch):
    """Contextual blame is applied before finalize; the deferred judge sees it and yields."""
    _no_patching(monkeypatch)
    _stub_judge(monkeypatch, passed=True, confidence=1.0)

    # `summary` is never written by anyone, and `answer` reads it.
    record = _run(
        monkeypatch,
        summarize_returns={"notes": "wrote something else"},
        semantic_judge=True,
        consumers={"summary": ["answer"]},
    )
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert "search" in verdict.failing_nodes, "origin is the first step without the field"
    assert "answer" not in verdict.failing_nodes, "the reader is the victim"


@pytest.mark.integration
def test_a_judge_pass_leaves_a_genuinely_clean_run_clean(monkeypatch):
    """Guards against a judge wiring that fails everything it touches."""
    _no_patching(monkeypatch)
    _stub_judge(monkeypatch, passed=True, confidence=1.0)

    record = _run(
        monkeypatch,
        summarize_returns={"summary": "a real summary of doc-1"},
        semantic_judge=True,
    )

    assert record.overall_status == "clean"
    assert evaluate_run(record).passed is True
