"""E2 / #129: allow_empty covers empty retrieval lists on the writer node's tools.

An empty list on a retrieval-named key (``hits``, ``docs``, …) is critical by
default — right for RAG. Wrong for sanctions screening, where ``hits: []`` is
the clean outcome. Declaring
``consumers={"screening": {"readers": [...], "allow_empty": True}}`` used to
only soften the contextual layer; the finding still fired on the tool key
``hits``. Now ``allow_empty`` on a field also softens empty retrieval lists in
the tool responses (and own update) of the node that writes it. A tool that
raised still fails hard.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

import argus
from argus.check import evaluate_run
from argus.inspector import inspect_tool_calls, inspect_tool_outputs
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _run(app, payload, consumers=None):
    recorder = ArgusRecorder(consumers=consumers, semantic_judge=False)
    recorder.attach(app).invoke(payload)
    record = load_run(recorder.session.run_id)
    return evaluate_run(record), record


def _sev(failures, field: str) -> str | None:
    for tf in failures:
        if tf.field_name == field:
            return tf.severity
    return None


# ── unit ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_empty_hits_critical_by_default():
    result = inspect_tool_outputs({"hits": []})
    assert _sev(result.tool_failures, "hits") == "critical"
    assert result.has_tool_failure


@pytest.mark.unit
def test_empty_hits_warns_when_allow_empty():
    result = inspect_tool_outputs({"hits": []}, allow_empty=True)
    assert _sev(result.tool_failures, "hits") == "warning"
    assert not result.has_tool_failure


@pytest.mark.unit
def test_tool_call_empty_hits_softened_with_allow_empty():
    hard = inspect_tool_calls([{"name": "sanctions_api", "output": {"hits": []}}])
    soft = inspect_tool_calls(
        [{"name": "sanctions_api", "output": {"hits": []}}],
        allow_empty=True,
    )
    assert _sev(hard, "sanctions_api.hits") == "critical"
    assert _sev(soft, "sanctions_api.hits") == "warning"


@pytest.mark.unit
def test_tool_error_stays_critical_even_with_allow_empty():
    failures = inspect_tool_calls(
        [{"name": "sanctions_api", "error": "TimeoutError('ofac down')"}],
        allow_empty=True,
    )
    assert len(failures) == 1
    assert failures[0].failure_type == "tool_error"
    assert failures[0].severity == "critical"


@pytest.mark.unit
def test_http_error_stays_critical_even_with_allow_empty():
    result = inspect_tool_outputs({"hits": [], "status_code": 503}, allow_empty=True)
    assert any(
        tf.failure_type == "error_response" and tf.severity == "critical"
        for tf in result.tool_failures
    )


# ── integration (issue guards) ───────────────────────────────────────────────


class KycState(TypedDict, total=False):
    name: str
    screening: list
    risk: str


class RagState(TypedDict, total=False):
    query: str
    docs: list
    answer: str


@pytest.mark.integration
def test_e2_undeclared_empty_docs_fails_ci():
    """Guard: RAG retriever returns docs: [] with nothing declared → fail."""

    def retrieve(state: RagState) -> dict:
        return {"docs": []}

    def answer(state: RagState) -> dict:
        return {"answer": "no sources"}

    g = StateGraph(RagState)
    g.add_node("retrieve", retrieve)
    g.add_node("answer", answer)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer", END)

    verdict, record = _run(g.compile(), {"query": "refunds"})
    assert verdict.passed is False
    assert any(
        f.type == "empty_result" and f.node == "retrieve" for f in record.findings
    ), record.findings


@pytest.mark.integration
def test_e2_sanctions_hits_clean_with_allow_empty():
    """Guard: sanctions screen hits: [] with allow_empty declared → clean."""

    def screen(state: KycState) -> dict:
        ok = argus.report_tool_call(
            "sanctions_api",
            input={"name": state["name"]},
            output={"hits": []},
        )
        assert ok is True
        return {"screening": [{"name": state["name"], "hits": []}]}

    def risk(state: KycState) -> dict:
        hits = (state.get("screening") or [{}])[0].get("hits") or []
        return {"risk": "low" if not hits else "high"}

    g = StateGraph(KycState)
    g.add_node("screen", screen)
    g.add_node("risk", risk)
    g.add_edge(START, "screen")
    g.add_edge("screen", "risk")
    g.add_edge("risk", END)

    consumers = {"screening": {"readers": ["risk"], "allow_empty": True}}
    verdict, record = _run(g.compile(), {"name": "Ada Lovelace"}, consumers)
    assert verdict.passed is True, record.findings
    # Soft flag may remain as a warning for argus show; must not gate CI.
    assert not any(f.severity == "critical" for f in record.findings), record.findings


@pytest.mark.integration
def test_e2_swallowed_tool_error_still_fails_with_allow_empty():
    """Guard: sanctions tool raises and the node swallows it → fail."""

    def screen(state: KycState) -> dict:
        ok = argus.report_tool_call(
            "sanctions_api",
            input={"name": state["name"]},
            error="TimeoutError('ofac down')",
        )
        assert ok is True
        # Node pretends a clean screen after swallowing the raise.
        return {"screening": [{"name": state["name"], "hits": []}]}

    def risk(state: KycState) -> dict:
        return {"risk": "low"}

    g = StateGraph(KycState)
    g.add_node("screen", screen)
    g.add_node("risk", risk)
    g.add_edge(START, "screen")
    g.add_edge("screen", "risk")
    g.add_edge("risk", END)

    consumers = {"screening": {"readers": ["risk"], "allow_empty": True}}
    verdict, record = _run(g.compile(), {"name": "Ada Lovelace"}, consumers)
    assert verdict.passed is False
    assert any(
        f.type == "tool_error" and f.node == "screen" for f in record.findings
    ), record.findings
