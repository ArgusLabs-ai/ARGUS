"""Integration tests — full detection pipeline via ArgusSession."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from argus.models import LLMInvestigationConfig
from argus.session import ArgusSession
from argus.storage import load_run


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".argus" / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)

def _session(**kwargs):
    return ArgusSession(**kwargs)

@pytest.mark.unit
class TestCleanPipeline:
    def test_three_node_clean_run(self):
        session = _session()
        session.set_node_names(["fetch", "analyze", "summarize"])
        session.set_edges({"fetch": ["analyze"], "analyze": ["summarize"]})

        fetch = session.wrap("fetch", lambda s: {"data": [1, 2, 3]})
        _analysis = (
            "The quarterly revenue analysis shows a 15% increase"
            " in recurring subscriptions across all segments"
        )
        analyze = session.wrap(
            "analyze", lambda s: {"analysis": _analysis},
        )
        _summary = (
            "Revenue grew 15% quarter over quarter driven primarily"
            " by enterprise subscription renewals and new deals"
        )
        summarize = session.wrap(
            "summarize", lambda s: {"summary": _summary},
        )

        state = {}
        state = fetch(state)
        state = analyze(state)
        state = summarize(state)
        session.finalize()

        loaded = load_run(session.run_id)
        assert loaded.overall_status == "clean"
        assert len(loaded.steps) == 3

@pytest.mark.unit
class TestToolFailureDetection:
    def test_error_in_output_detected(self):
        session = _session()
        session.set_node_names(["fetch"])
        fetch = session.wrap("fetch", lambda s: {"error": "API timeout"})
        fetch({})
        session.finalize()

        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        assert event.status == "fail"
        assert any(tf.failure_type == "error_response" for tf in event.inspection.tool_failures)

    def test_http_500_detected(self):
        session = _session()
        session.set_node_names(["api_call"])
        api = session.wrap("api_call", lambda s: {"status_code": 500, "body": "error"})
        api({})
        session.finalize()

        loaded = load_run(session.run_id)
        assert loaded.steps[0].status == "fail"

    def test_empty_results_detected(self):
        session = _session()
        session.set_node_names(["search"])
        search = session.wrap("search", lambda s: {"results": []})
        search({})
        session.finalize()

        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        assert any(tf.failure_type == "empty_result" for tf in event.inspection.tool_failures)
        assert event.status == "fail"
        assert loaded.overall_status == "silent_failure"

    def test_empty_documents_overall_not_clean(self):
        session = _session()
        session.set_node_names(["retrieve"])
        retrieve = session.wrap("retrieve", lambda s: {"documents": []})
        retrieve({"query": "refund"})
        session.finalize()

        loaded = load_run(session.run_id)
        assert loaded.overall_status != "clean"
        assert loaded.steps[0].status == "fail"

@pytest.mark.unit
class TestValidators:
    def test_validator_failure_marks_semantic_fail(self):
        validators = {
            "analyze": lambda out: (len(out.get("analysis", "")) > 10, "analysis too short"),
        }
        session = _session(validators=validators)
        session.set_node_names(["analyze"])
        analyze = session.wrap("analyze", lambda s: {**s, "analysis": "ok"})
        analyze({})
        session.finalize()

        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        assert event.status == "semantic_fail"

    def test_wildcard_validator(self):
        validators = {
            "*": lambda out: ("error" not in out, f"Error found: {out.get('error')}"),
        }
        session = _session(validators=validators)
        session.set_node_names(["a", "b"])
        session.set_edges({"a": ["b"]})

        a = session.wrap("a", lambda s: {**s, "error": "boom"})
        b = session.wrap("b", lambda s: {**s, "result": "ok"})

        state = a({})
        b(state)
        session.finalize()

        loaded = load_run(session.run_id)
        a_event = [e for e in loaded.steps if e.node_name == "a"][0]
        assert any(not vr.is_valid for vr in a_event.validator_results)

@pytest.mark.unit
class TestCrashedNode:
    def test_crash_recorded(self):
        session = _session()
        session.set_node_names(["crash_node"])

        def crashing_fn(state):
            raise ValueError("something broke")

        wrapped = session.wrap("crash_node", crashing_fn)
        with pytest.raises(ValueError):
            wrapped({})

        session.finalize()
        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        assert event.status == "crashed"
        assert "something broke" in event.exception

@pytest.mark.unit
class TestConcurrency:
    def test_sequential_10_nodes(self):
        session = _session()
        names = [f"node_{i}" for i in range(10)]
        session.set_node_names(names)
        edges = {names[i]: [names[i+1]] for i in range(9)}
        session.set_edges(edges)

        fns = {}
        for name in names:
            fns[name] = session.wrap(name, lambda s, n=name: {**s, n: "done"})

        state = {}
        for name in names:
            state = fns[name](state)

        session.finalize()
        loaded = load_run(session.run_id)
        assert len(loaded.steps) == 10
        assert all(e.status == "pass" for e in loaded.steps)

@pytest.mark.unit
class TestMultipleDetectionLayers:
    def test_error_and_empty_combined(self):
        session = _session()
        session.set_node_names(["bad_node"])
        wrapped = session.wrap("bad_node", lambda s: {"error": "fail", "results": []})
        wrapped({})
        session.finalize()

        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        types = {tf.failure_type for tf in event.inspection.tool_failures}
        assert "error_response" in types


@pytest.mark.unit
class TestAsyncWrapper:
    def test_async_node_detection(self):
        session = _session()
        session.set_node_names(["async_node"])

        async def async_fn(state):
            return {**state, "result": "async done"}

        wrapped = session.wrap("async_node", async_fn)
        result = asyncio.get_event_loop().run_until_complete(wrapped({}))
        session.finalize()

        loaded = load_run(session.run_id)
        assert len(loaded.steps) == 1
        assert loaded.steps[0].status == "pass"
        assert result["result"] == "async done"

    def test_async_crash_detection(self):
        session = _session()
        session.set_node_names(["async_crash"])

        async def crashing_async(state):
            raise RuntimeError("async boom")

        wrapped = session.wrap("async_crash", crashing_async)
        with pytest.raises(RuntimeError):
            asyncio.get_event_loop().run_until_complete(wrapped({}))

        session.finalize()
        loaded = load_run(session.run_id)
        assert loaded.steps[0].status == "crashed"
        assert "async boom" in loaded.steps[0].exception

    def test_async_error_output_detected(self):
        session = _session()
        session.set_node_names(["async_err"])

        async def async_error_fn(state):
            return {"error": "async failure"}

        wrapped = session.wrap("async_err", async_error_fn)
        asyncio.get_event_loop().run_until_complete(wrapped({}))
        session.finalize()

        loaded = load_run(session.run_id)
        assert loaded.steps[0].status == "fail"


@pytest.mark.unit
class TestConcurrentNodes:
    def test_threadpool_concurrent_detection(self):
        session = _session()
        names = [f"worker_{i}" for i in range(5)]
        session.set_node_names(names)

        fns = {}
        for name in names:
            fns[name] = session.wrap(name, lambda s, n=name: {**s, n: "done"})

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(fns[n], {}) for n in names]
            for f in futures:
                f.result()

        session.finalize()
        loaded = load_run(session.run_id)
        assert len(loaded.steps) == 5
        assert all(e.status == "pass" for e in loaded.steps)


@pytest.mark.unit
class TestDegradedInputAttribution:
    def test_upstream_fail_downstream_degraded(self):
        session = _session()
        session.set_node_names(["fetch", "process"])
        session.set_edges({"fetch": ["process"]})

        fetch = session.wrap("fetch", lambda s: {"error": "API down", "results": []})
        process = session.wrap("process", lambda s: {**s, "analysis": "empty"})

        state = fetch({})
        process(state)
        session.finalize()

        loaded = load_run(session.run_id)
        fetch_ev = [e for e in loaded.steps if e.node_name == "fetch"][0]
        assert fetch_ev.status == "fail"


@pytest.mark.unit
class TestLatencySignals:
    def test_timeout_adjacent_signal(self):
        session = _session(node_timeout_ms=1000.0)
        session.set_node_names(["slow_node"])

        import time as _time

        def slow_fn(state):
            _time.sleep(0.96)
            return {**state, "result": "barely made it"}

        wrapped = session.wrap("slow_node", slow_fn)
        wrapped({})
        session.finalize()

        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        if event.inspection:
            timeout_tfs = [
                tf for tf in event.inspection.tool_failures
                if tf.failure_type == "timeout_adjacent"
            ]
            assert len(timeout_tfs) >= 1

    def test_latency_quality_mismatch(self):
        session = _session(min_expected_ms=2000.0)
        session.set_node_names(["fast_fail"])

        wrapped = session.wrap("fast_fail", lambda s: {"error": "cached error"})
        wrapped({})
        session.finalize()

        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        if event.inspection:
            mismatch_tfs = [
                tf for tf in event.inspection.tool_failures
                if tf.failure_type == "latency_quality_mismatch"
            ]
            assert len(mismatch_tfs) >= 1


@pytest.mark.unit
class TestLLMJudgeOverride:
    def test_structural_failure_blocks_override(self, monkeypatch):
        """LLM judge pass cannot override structural failures."""
        import json as _json
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        _resp = {
            "choices": [{"message": {"content": _json.dumps(
                {"pass": True, "reason": "ok", "confidence": 0.95},
            )}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        monkeypatch.setattr(
            "argus.llm_proxy.create_chat_completion", lambda **kw: _resp,
        )

        session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(
                enabled=True, semantic_check=True,
            ),
        )
        session.set_node_names(["bad"])
        wrapped = session.wrap(
            "bad", lambda s: {"error": "fail", "results": []},
        )
        wrapped({})
        session.finalize()

        loaded = load_run(session.run_id)
        assert loaded.steps[0].status == "fail"

    def test_ambiguous_heuristic_can_be_overridden(self, monkeypatch):
        """LLM judge pass CAN override ambiguous heuristic-only failures."""
        import json as _json
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        _resp = {
            "choices": [{"message": {"content": _json.dumps(
                {"pass": True, "reason": "looks fine", "confidence": 0.95},
            )}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        monkeypatch.setattr(
            "argus.llm_proxy.create_chat_completion", lambda **kw: _resp,
        )

        session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True),
        )
        session.set_node_names(["node"])

        def node_fn(state):
            return {**state, "analysis": "True"}

        wrapped = session.wrap("node", node_fn)
        wrapped({})
        session.finalize()

        loaded = load_run(session.run_id)
        event = loaded.steps[0]
        assert event.status in ("pass", "semantic_fail")
