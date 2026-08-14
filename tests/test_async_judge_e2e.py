"""End-to-end tests for async LLM judge with real LangGraph pipelines.

These tests build actual LangGraph StateGraphs, inject various failure
modes, run them through ArgusWatcher, and verify detection works correctly
with the deferred (background-thread) LLM judge.
"""
from __future__ import annotations

import json
import time
from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph

from argus.storage import load_run
from argus.watcher import ArgusWatcher


class PipelineState(TypedDict, total=False):
    query: str
    context: str
    answer: str
    score: float
    sources: list[str]


# ── Helper nodes ────────────────────────────────────────────────────────────

def fetch_context(state: PipelineState) -> PipelineState:
    return {"context": f"docs about: {state.get('query', '')}"}


def generate_answer(state: PipelineState) -> PipelineState:
    return {"answer": f"Based on {state.get('context', '')}, the answer is 42."}


def score_answer(state: PipelineState) -> PipelineState:
    return {"score": 0.85, "sources": ["doc1.pdf", "doc2.pdf"]}


# ── Broken nodes ────────────────────────────────────────────────────────────

def fetch_context_empty(state: PipelineState) -> PipelineState:
    return {"context": ""}


def generate_answer_placeholder(state: PipelineState) -> PipelineState:
    return {"answer": "I don't know"}


def generate_answer_error(state: PipelineState) -> PipelineState:
    return {"answer": "", "error": "model timeout", "status_code": 500}


def score_missing_fields(state: PipelineState) -> PipelineState:
    return {"score": 0.0}


# ── Mock LLM ────────────────────────────────────────────────────────────────

def _mock_llm_pass(**kwargs):
    return {
        "choices": [{"message": {"content": json.dumps({
            "pass": True, "reason": "output looks coherent", "confidence": 0.9,
        })}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _mock_llm_fail(**kwargs):
    return {
        "choices": [{"message": {"content": json.dumps({
            "pass": False, "reason": "output is nonsensical", "confidence": 0.85,
        })}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _build_and_run(nodes: dict, input_state: dict | None = None):
    """Build a linear graph, run it through ArgusWatcher, return (watcher, events, record)."""
    g = StateGraph(PipelineState)
    names = list(nodes.keys())
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.set_entry_point(names[0])
    for i in range(len(names) - 1):
        g.add_edge(names[i], names[i + 1])
    g.add_edge(names[-1], END)

    watcher = ArgusWatcher(g, semantic_judge=True)
    app = g.compile()
    app.invoke(input_state or {"query": "What is ARGUS?"})
    watcher.finalize()

    events = watcher._session._events
    run_id = watcher._session.run_id
    record = load_run(run_id)
    return watcher, events, record


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


# ── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestAsyncJudgeE2E:

    def test_clean_pipeline_passes(self):
        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer,
            "score": score_answer,
        })
        assert record.overall_status == "clean"
        assert len(events) == 3
        for e in events:
            assert e.status == "pass"

    def test_empty_output_detected(self):
        _, events, record = _build_and_run({
            "fetch": fetch_context_empty,
            "generate": generate_answer,
            "score": score_answer,
        })
        fetch_event = next(e for e in events if e.node_name == "fetch")
        assert fetch_event.inspection is not None
        has_empty = bool(fetch_event.inspection.empty_fields)
        assert has_empty or fetch_event.status != "pass"

    def test_error_in_output_detected(self):
        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer_error,
            "score": score_answer,
        })
        gen_event = next(e for e in events if e.node_name == "generate")
        assert gen_event.status in ("fail", "semantic_fail")
        assert gen_event.inspection is not None
        assert gen_event.inspection.has_tool_failure

    def test_placeholder_detected(self, monkeypatch):
        """'I don't know' is a known placeholder — should be caught when LLM judge runs."""
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _mock_llm_fail)

        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer_placeholder,
            "score": score_answer,
        })
        gen_event = next(e for e in events if e.node_name == "generate")
        # With LLM saying fail, this should be caught
        assert gen_event.status == "semantic_fail"

    def test_crash_propagates(self):
        def crash_node(state):
            raise ValueError("boom")

        g = StateGraph(PipelineState)
        g.add_node("fetch", fetch_context)
        g.add_node("generate", crash_node)
        g.add_node("score", score_answer)
        g.set_entry_point("fetch")
        g.add_edge("fetch", "generate")
        g.add_edge("generate", "score")
        g.add_edge("score", END)

        watcher = ArgusWatcher(g, semantic_judge=True)
        app = g.compile()
        try:
            app.invoke({"query": "test"})
        except Exception:
            pass
        watcher.finalize()

        events = watcher._session._events
        gen_event = next(e for e in events if e.node_name == "generate")
        assert gen_event.status == "crashed"
        assert "boom" in gen_event.exception

    def test_multiple_failures_all_detected(self):
        _, events, record = _build_and_run({
            "fetch": fetch_context_empty,
            "generate": generate_answer_error,
            "score": score_missing_fields,
        })
        assert record.overall_status != "clean"
        failed = [e for e in events if e.status not in ("pass", "skipped")]
        labels = [f"{e.node_name}:{e.status}" for e in events]
        assert len(failed) >= 2, f"Expected ≥2 failures, got {labels}"


@pytest.mark.integration
class TestAsyncJudgeWithMockedLLM:

    def _enable_llm(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)

    def test_async_judge_applies_pass_verdict(self, monkeypatch):
        self._enable_llm(monkeypatch)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _mock_llm_pass)

        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer,
            "score": score_answer,
        })
        assert record.overall_status == "clean"
        for e in events:
            assert e.status == "pass"
            if e.semantic_check is not None:
                assert e.semantic_check.passed is True

    def test_async_judge_applies_fail_verdict(self, monkeypatch):
        self._enable_llm(monkeypatch)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _mock_llm_fail)

        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer,
            "score": score_answer,
        })
        assert record.overall_status != "clean"
        semantic_fails = [e for e in events if e.status == "semantic_fail"]
        assert len(semantic_fails) >= 1

    def test_async_judge_concurrent_calls(self, monkeypatch):
        """Verify multiple LLM calls fire and complete."""
        self._enable_llm(monkeypatch)
        call_count = {"n": 0}

        def _counting_llm(**kwargs):
            call_count["n"] += 1
            time.sleep(0.1)
            return _mock_llm_pass(**kwargs)

        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _counting_llm)

        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer,
            "score": score_answer,
        })
        # 3 per-node judge calls + 1 per-run investigator call = 4
        assert call_count["n"] >= 3, f"Expected ≥3 LLM calls, got {call_count['n']}"
        assert all(e.semantic_check is not None for e in events)

    def test_judge_failure_warn_doesnt_crash(self, monkeypatch):
        self._enable_llm(monkeypatch)

        def _broken_llm(**kwargs):
            raise ConnectionError("network down")

        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _broken_llm)

        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer,
            "score": score_answer,
        })
        assert record is not None
        assert len(events) == 3

    def test_structural_failure_not_overridden_by_async_judge(self, monkeypatch):
        """Even with async LLM judge saying pass, structural failures stick."""
        self._enable_llm(monkeypatch)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _mock_llm_pass)

        _, events, record = _build_and_run({
            "fetch": fetch_context,
            "generate": generate_answer_error,
            "score": score_answer,
        })
        gen_event = next(e for e in events if e.node_name == "generate")
        # LLM said pass, but error key + 500 status = structural failure → not overridden
        assert gen_event.status in ("fail", "semantic_fail", "degraded_input")
        assert gen_event.inspection.has_tool_failure


@pytest.mark.integration
class TestAsyncJudgeFiveNodePipeline:

    def test_five_node_pipeline_failures_detected(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _mock_llm_pass)

        class BigState(TypedDict, total=False):
            query: str
            raw_data: str
            cleaned: str
            analyzed: str
            summary: str
            final: str

        def step1(s): return {"raw_data": "raw content"}
        def step2(s): return {"cleaned": ""}  # silent failure: empty
        def step3(s): return {"analyzed": "analysis of " + s.get("cleaned", "")}
        def step4(s): return {"summary": "summary", "error": "partial failure"}
        def step5(s): return {"final": "done"}

        g = StateGraph(BigState)
        for name, fn in [("ingest", step1), ("clean", step2), ("analyze", step3),
                         ("summarize", step4), ("output", step5)]:
            g.add_node(name, fn)
        g.set_entry_point("ingest")
        g.add_edge("ingest", "clean")
        g.add_edge("clean", "analyze")
        g.add_edge("analyze", "summarize")
        g.add_edge("summarize", "output")
        g.add_edge("output", END)

        watcher = ArgusWatcher(g, semantic_judge=True)
        app = g.compile()
        app.invoke({"query": "test"})
        watcher.finalize()

        events = watcher._session._events
        run_id = watcher._session.run_id
        record = load_run(run_id)

        assert record is not None
        assert len(events) == 5
        assert record.overall_status != "clean"

        # "summarize" should be flagged (error key)
        sum_event = next(e for e in events if e.node_name == "summarize")
        assert sum_event.inspection is not None
        assert sum_event.inspection.has_tool_failure

    def test_five_node_all_clean(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _mock_llm_pass)

        class BigState(TypedDict, total=False):
            query: str
            a: str
            b: str
            c: str
            d: str
            e: str

        g = StateGraph(BigState)
        for name in ["n1", "n2", "n3", "n4", "n5"]:
            key = name[0]
            g.add_node(name, lambda state, k=key: {k: f"output_{k}"})
        g.set_entry_point("n1")
        for a, b in [("n1", "n2"), ("n2", "n3"), ("n3", "n4"), ("n4", "n5")]:
            g.add_edge(a, b)
        g.add_edge("n5", END)

        watcher = ArgusWatcher(g, semantic_judge=True)
        app = g.compile()
        app.invoke({"query": "test"})
        watcher.finalize()

        events = watcher._session._events
        run_id = watcher._session.run_id
        record = load_run(run_id)

        assert record.overall_status == "clean"
        assert all(e.status == "pass" for e in events)
        assert len(events) == 5
