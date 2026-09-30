"""Unit tests for argus.anomaly_detector — all 8 behavioral anomaly checks."""
import pytest

from argus.anomaly_detector import (
    BEHAVIOR_PROFILES,
    _check_abnormal_tool_response,
    _check_generic_response,
    _check_incomplete_reasoning,
    _check_info_density,
    _check_length_collapse,
    _check_repetitive_filler,
    _check_shallow_empty,
    _check_structural_malformation,
    detect_anomalies,
    infer_behavior_type,
    resolve_behavior_type,
)
from argus.models import BehaviorConfig


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

# ── BA-001: Length collapse/explosion ────────────────────────────────────────

@pytest.mark.unit
class TestBA001LengthCollapse:
    def test_collapse_below_range(self):
        profile = BEHAVIOR_PROFILES["detailed_text"]  # range 100-50000
        signal = _check_length_collapse({"x": "hi"}, profile, "detailed_text")
        assert signal is not None
        assert signal.anomaly_id == "BA-001"
        assert "collapse" in signal.reason

    def test_explosion_above_range(self):
        profile = BEHAVIOR_PROFILES["classification"]  # range 5-500
        big_output = {"text": "x" * 10000}
        signal = _check_length_collapse(big_output, profile, "classification")
        assert signal is not None
        assert "explosion" in signal.reason

    def test_within_range_ok(self):
        profile = BEHAVIOR_PROFILES["structured_json"]  # range 20-50000
        signal = _check_length_collapse({"key": "value" * 10}, profile, "structured_json")
        assert signal is None

# ── BA-002: Repetitive filler ────────────────────────────────────────────────

@pytest.mark.unit
class TestBA002RepetitiveFiller:
    def test_repetitive_text_detected(self):
        repetitive = " ".join(["the quick brown fox"] * 30)
        signal = _check_repetitive_filler({"text": repetitive})
        assert signal is not None
        assert signal.anomaly_id == "BA-002"

    def test_normal_text_ok(self):
        diverse_text = (
            "The market showed strong gains today with tech stocks leading. "
            "Healthcare sectors remained stable while energy companies declined. "
            "International markets were mixed with European indices rising. "
            "Bond yields continued their upward trajectory amid inflation concerns."
        )
        signal = _check_repetitive_filler({"text": diverse_text})
        assert signal is None

    def test_short_text_skipped(self):
        signal = _check_repetitive_filler({"text": "hello"})
        assert signal is None

# ── BA-003: Info density ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestBA003InfoDensity:
    def test_low_density_detected(self):
        text = " ".join(["the"] * 100)
        profile = BEHAVIOR_PROFILES["detailed_text"]
        signal = _check_info_density({"text": text}, profile, "detailed_text")
        assert signal is not None
        assert signal.anomaly_id == "BA-003"

    def test_normal_density_ok(self):
        text = " ".join(f"word{i}" for i in range(50))
        profile = BEHAVIOR_PROFILES["detailed_text"]
        signal = _check_info_density({"text": text}, profile, "detailed_text")
        assert signal is None

# ── BA-004: Generic response ─────────────────────────────────────────────────

@pytest.mark.unit
class TestBA004GenericResponse:
    def test_generic_phrases_detected(self):
        output = {
            "response": "I'm sorry, I cannot help with that request.",
            "fallback": "No data available for your query.",
            "status": "Unable to process your request at this time.",
        }
        signal = _check_generic_response(output)
        assert signal is not None
        assert signal.anomaly_id == "BA-004"

    def test_specific_content_ok(self):
        output = {
            "analysis": "Tesla stock rose 5.3% on strong Q3 earnings.",
            "recommendation": "Consider a buy position based on technical indicators.",
        }
        signal = _check_generic_response(output)
        assert signal is None

    def test_below_30pct_threshold(self):
        output = {
            "error_msg": "I'm sorry for the inconvenience.",
            "result1": "Specific analysis data point one.",
            "result2": "Another concrete result with numbers 42.",
            "result3": "Third result with clear specifics.",
        }
        signal = _check_generic_response(output)
        assert signal is None  # only 1/4 = 25% < 30%

# ── BA-005: Structural malformation ──────────────────────────────────────────

@pytest.mark.unit
class TestBA005StructuralMalformation:
    def test_flat_when_nested_expected(self):
        profile = BEHAVIOR_PROFILES["structured_json"]  # expects_nested=True
        signal = _check_structural_malformation({"flat_key": "value"}, profile, "structured_json")
        assert signal is not None
        assert signal.anomaly_id == "BA-005"

    def test_missing_list_field(self):
        profile = BEHAVIOR_PROFILES["retrieval_result"]  # expects_list_field=True
        signal = _check_structural_malformation({"text": "just text"}, profile, "retrieval_result")
        assert signal is not None

    def test_correct_structure_ok(self):
        profile = BEHAVIOR_PROFILES["retrieval_result"]
        signal = _check_structural_malformation(
            {"results": [{"doc": "x"}]}, profile, "retrieval_result"
        )
        assert signal is None

# ── BA-006: Shallow empty ────────────────────────────────────────────────────

@pytest.mark.unit
class TestBA006ShallowEmpty:
    def test_majority_empty_fields(self):
        output = {"a": None, "b": "", "c": {}, "d": "only value"}
        profile = BEHAVIOR_PROFILES["structured_json"]
        signal = _check_shallow_empty(output, profile, "structured_json")
        assert signal is not None
        assert signal.anomaly_id == "BA-006"

    def test_empty_output_with_input_passthrough(self):
        """Pass-through node: empty output with input_state is NOT flagged."""
        profile = BEHAVIOR_PROFILES["structured_json"]
        signal = _check_shallow_empty({}, profile, "structured_json", input_state={"x": 1})
        assert signal is None

    def test_empty_output_without_input(self):
        profile = BEHAVIOR_PROFILES["structured_json"]
        signal = _check_shallow_empty({}, profile, "structured_json", input_state=None)
        assert signal is not None
        assert signal.severity == "critical"

# ── BA-007: Incomplete reasoning ─────────────────────────────────────────────

@pytest.mark.unit
class TestBA007IncompleteReasoning:
    def test_single_step_chain(self):
        signal = _check_incomplete_reasoning(
            {"steps": ["only one step"]}, "reasoning_chain"
        )
        assert signal is not None
        assert signal.anomaly_id == "BA-007"

    def test_truncated_last_step(self):
        signal = _check_incomplete_reasoning(
            {"steps": [
                "First step with detailed analysis of the problem at hand.",
                "Second step with thorough investigation of all factors.",
                "Th",  # truncated — < 20% of avg
            ]},
            "reasoning_chain",
        )
        assert signal is not None
        assert "truncated" in signal.reason

    def test_non_reasoning_type_skipped(self):
        signal = _check_incomplete_reasoning(
            {"steps": ["one"]}, "classification"
        )
        assert signal is None

    def test_multi_step_ok(self):
        signal = _check_incomplete_reasoning(
            {"steps": [
                "First step with analysis.",
                "Second step with conclusion.",
                "Third step with recommendation.",
            ]},
            "reasoning_chain",
        )
        assert signal is None

# ── BA-008: Abnormal tool response ───────────────────────────────────────────

@pytest.mark.unit
class TestBA008AbnormalToolResponse:
    def test_no_text_content(self):
        signal = _check_abnormal_tool_response(
            {"status": 200, "count": 5}, "tool_output"
        )
        assert signal is not None
        assert signal.anomaly_id == "BA-008"

    def test_identical_repeated_items(self):
        signal = _check_abnormal_tool_response(
            {"results": [{"a": 1}, {"a": 1}, {"a": 1}], "note": "some text here"}, "tool_output"
        )
        assert signal is not None
        assert "identical" in signal.reason or "no text" in signal.reason

    def test_non_tool_type_skipped(self):
        signal = _check_abnormal_tool_response(
            {"status": 200}, "classification"
        )
        assert signal is None

# ── Behavior type inference ──────────────────────────────────────────────────

@pytest.mark.unit
class TestBehaviorTypeInference:
    def test_retrieval_result(self):
        assert infer_behavior_type({"results": [{"doc": "x"}]}) == "retrieval_result"

    def test_classification(self):
        assert infer_behavior_type({"label": "positive"}) == "classification"

    def test_reasoning_chain(self):
        output = {"steps": [{"thought": "a"}, {"thought": "b"}], "nested": {"deep": True}}
        assert infer_behavior_type(output) == "reasoning_chain"

    def test_empty_output(self):
        assert infer_behavior_type({}) == "structured_json"

# ── Behavior resolution 3-tier ───────────────────────────────────────────────

@pytest.mark.unit
class TestBehaviorResolution:
    def test_node_override_wins(self):
        config = BehaviorConfig(
            default_behavior_type="classification",
            node_behaviors={"my_node": "retrieval_result"},
        )
        assert resolve_behavior_type("my_node", {}, config) == "retrieval_result"

    def test_pipeline_default(self):
        config = BehaviorConfig(default_behavior_type="detailed_text")
        assert resolve_behavior_type("any_node", {}, config) == "detailed_text"

    def test_auto_inferred_fallback(self):
        assert resolve_behavior_type("any_node", {"label": "pos"}, None) == "classification"

# ── Full detect_anomalies ────────────────────────────────────────────────────

@pytest.mark.unit
class TestDetectAnomalies:
    def test_clean_output_no_anomalies(self):
        text = (
            "The market showed strong gains today with tech stocks leading. "
            "Healthcare sectors remained stable while energy companies declined. "
            "International markets were mixed with European indices rising steadily. "
            "Bond yields continued their upward trajectory amid inflation concerns. "
            "Analysts expect continued volatility in the coming weeks ahead."
        )
        bt, signals = detect_anomalies("node", {"analysis": text})
        assert signals == []

    def test_multiple_anomalies_returned(self):
        bt, signals = detect_anomalies("node", {
            "a": "I'm sorry, I cannot help.",
            "b": None,
            "c": "",
        })
        assert len(signals) >= 1

# ── chat_response / code_generation profiles ─────────────────────────────────

@pytest.mark.unit
class TestChatResponseProfile:
    def test_clean_chat_reply_no_anomalies(self):
        config = BehaviorConfig(default_behavior_type="chat_response")
        reply = (
            "Sure, I can help with that. The quarterly report shows revenue "
            "grew 12% year over year, driven mostly by the new enterprise tier. "
            "Support tickets dropped 8% after the onboarding flow update."
        )
        bt, signals = detect_anomalies("chat_node", {"message": reply}, config)
        assert bt == "chat_response"
        assert signals == []

@pytest.mark.unit
class TestCodeGenerationProfile:
    def test_clean_code_snippet_no_anomalies(self):
        config = BehaviorConfig(default_behavior_type="code_generation")
        snippet = (
            "def fibonacci(n):\n"
            "    if n <= 1:\n"
            "        return n\n"
            "    a, b = 0, 1\n"
            "    for _ in range(n - 1):\n"
            "        a, b = b, a + b\n"
            "    return b\n"
        )
        bt, signals = detect_anomalies("code_node", {"code": snippet}, config)
        assert bt == "code_generation"
        assert signals == []

    def test_indented_class_no_repetition_false_positive(self):
        config = BehaviorConfig(default_behavior_type="code_generation")
        snippet = (
            "class Point:\n"
            "    def __init__(self, x, y):\n"
            "        self.x = x\n"
            "        self.y = y\n\n"
            "    def __repr__(self):\n"
            "        return f'Point({self.x}, {self.y})'\n\n"
            "    def __eq__(self, other):\n"
            "        return self.x == other.x and self.y == other.y\n"
        )
        bt, signals = detect_anomalies("code_node", {"code": snippet}, config)
        assert bt == "code_generation"
        assert signals == []
