"""Stress tests and edge cases for the ARGUS detection pipeline."""
import time

import pytest

from argus.anomaly_detector import detect_anomalies
from argus.heuristic_engine import scan_execution_output
from argus.inspector import inspect_tool_outputs, inspect_transition
from argus.session import ArgusSession
from argus.storage import load_run


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".argus" / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)

@pytest.mark.unit
class TestLargeOutputs:
    def test_1mb_dict_completes(self):
        big = {f"field_{i}": f"value_{i}" * 100 for i in range(1000)}
        t0 = time.perf_counter()
        inspect_tool_outputs(big)
        elapsed = time.perf_counter() - t0
        # 300s keeps the regression guard (pre-fix pathology was 1051s, >3x
        # over) while tolerating slow/loaded CI machines — the healthy run
        # measured 68s on the slowest machine observed (2026-09-23 sweep),
        # so 300s trips only on a >4x regression, not on machine speed.
        assert elapsed < 300.0, f"Took {elapsed:.1f}s (was 1051s before fix)"

    def test_1000_field_output(self):
        output = {f"key_{i}": f"data_{i}" for i in range(1000)}
        result = inspect_tool_outputs(output)
        assert result is not None

@pytest.mark.unit
class TestDeepNesting:
    def test_50_levels_no_stackoverflow(self):
        nested = {"value": "deep leaf"}
        for i in range(50):
            nested = {f"level_{i}": nested}
        signals = scan_execution_output(nested, max_depth=10)
        depth_signals = [s for s in signals if s.sig_id == "DEPTH-LIMIT"]
        assert len(depth_signals) >= 1

@pytest.mark.unit
class TestLargeStrings:
    def test_input_echo_10kb(self):
        large = "x" * 10_000
        t0 = time.perf_counter()
        inspect_tool_outputs({"output": large}, input_state={"input": large})
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0, f"Took {elapsed:.1f}s on 10KB strings"

@pytest.mark.unit
class TestUnicodeEdgeCases:
    def test_emoji_in_output(self):
        result = inspect_tool_outputs({"text": "Analysis complete 🎉✅ Results look good 📊"})
        assert result is not None

    def test_cjk_characters(self):
        result = inspect_tool_outputs({"text": "分析完了。結果は良好です。"})
        assert result is not None

    def test_zero_width_chars(self):
        result = inspect_tool_outputs({"text": "hello​​world​"})
        assert result is not None

    def test_mixed_scripts(self):
        result = inspect_tool_outputs({
            "text": "Results: 42 — análisis completado — 分析完了 — Результат"
        })
        assert result is not None

@pytest.mark.unit
class TestEdgeCaseInputs:
    def test_none_output_via_inspect_transition(self):
        result = inspect_transition("node", None, {}, [])
        assert result is not None
        assert result.severity == "ok"

    def test_empty_output_dict(self):
        result = inspect_tool_outputs({})
        assert result is not None
        assert result.severity == "ok"

    def test_nested_none_values(self):
        result = inspect_tool_outputs({
            "a": None, "b": {"c": None}, "d": [None, None]
        })
        assert result is not None

@pytest.mark.unit
class TestAnomalyDetectorStress:
    def test_large_output_anomaly_detection(self):
        big = {f"field_{i}": "value " * 50 for i in range(100)}
        bt, signals = detect_anomalies("node", big)
        assert bt is not None

    def test_empty_strings_output(self):
        output = {f"key_{i}": "" for i in range(20)}
        bt, signals = detect_anomalies("node", output)
        assert any(s.anomaly_id == "BA-006" for s in signals)

@pytest.mark.unit
class TestRapidSequentialRuns:
    def test_50_runs_no_state_leakage(self):
        for i in range(50):
            session = ArgusSession()
            session.set_node_names(["a"])
            wrapped = session.wrap("a", lambda state, idx=i: {**state, "x": idx})
            wrapped({})
            session.finalize()
            loaded = load_run(session.run_id)
            assert len(loaded.steps) == 1
