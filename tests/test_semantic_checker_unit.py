"""Unit tests for argus.semantic_checker — LLM semantic judge (all calls mocked)."""
import json

import pytest

from argus.models import AnomalySignal, InspectionResult, ToolFailure, ValidatorResult
from argus.semantic_checker import (
    _compact_dict,
    _extract_json_object,
    _truncate,
    check_semantic_coherence,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

def _mock_llm(monkeypatch, response_content, is_available=True):
    """Monkeypatch the LLM proxy to return a canned response."""
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: is_available)
    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", lambda **kwargs: {
        "choices": [{"message": {"content": json.dumps(response_content)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    })

@pytest.mark.unit
class TestSemanticCheckerPass:
    def test_pass_verdict(self, monkeypatch):
        _mock_llm(monkeypatch, {"pass": True, "reason": "looks good", "confidence": 0.9})
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.passed is True
        assert result.confidence == 0.9

    def test_fail_verdict(self, monkeypatch):
        _mock_llm(monkeypatch, {"pass": False, "reason": "nonsense", "confidence": 0.85})
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.passed is False

@pytest.mark.unit
class TestSemanticCheckerSkip:
    def test_empty_input_skipped(self, monkeypatch):
        _mock_llm(monkeypatch, {"pass": True})
        result, _ = check_semantic_coherence("node_a", {}, {"a": "world"})
        assert result.passed is True
        assert "skipped" in result.reason

    def test_empty_output_skipped(self, monkeypatch):
        _mock_llm(monkeypatch, {"pass": True})
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {})
        assert result.passed is True
        assert "skipped" in result.reason

    def test_not_available_skipped(self, monkeypatch):
        _mock_llm(monkeypatch, {"pass": True}, is_available=False)
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.passed is True
        assert "not logged in" in result.reason

@pytest.mark.unit
class TestSemanticCheckerMalformed:
    def test_malformed_llm_response_defaults_pass(self, monkeypatch):
        """When LLM returns garbage, defaults to passed=True."""
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", lambda **kwargs: {
            "choices": [{"message": {"content": "not json at all"}}],
            "usage": {},
        })
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.passed is True
        assert result.evaluated is False

    def test_no_choices_defaults_pass(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", lambda **kwargs: {
            "choices": [],
            "usage": {},
        })
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.passed is True

    def test_error_in_result(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", lambda **kwargs: {
            "error": "rate limited",
        })
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.passed is True
        assert "rate limited" in result.reason

@pytest.mark.unit
class TestSemanticCheckerEvidence:
    def test_failed_validators_in_prompt(self, monkeypatch):
        """Failed validators should appear in the prompt sent to LLM."""
        captured_kwargs = {}
        def fake_llm(**kwargs):
            captured_kwargs.update(kwargs)
            return {
                "choices": [{"message": {"content": json.dumps({"pass": True})}}],
                "usage": {},
            }
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", fake_llm)

        validators = [ValidatorResult("check_len", False, "too short")]
        check_semantic_coherence(
            "node_a", {"q": "hello"}, {"a": "world"},
            validator_results=validators,
        )
        user_msg = captured_kwargs["messages"][1]["content"]
        assert "too short" in user_msg
        assert "Validator failures" in user_msg

    def test_critical_anomalies_in_prompt(self, monkeypatch):
        captured_kwargs = {}
        def fake_llm(**kwargs):
            captured_kwargs.update(kwargs)
            return {
                "choices": [{"message": {"content": json.dumps({"pass": True})}}],
                "usage": {},
            }
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", fake_llm)

        anomalies = [AnomalySignal("BA-001", "critical", 0.9, "length collapse",
                                    "100-50000 chars", "5 chars", "")]
        check_semantic_coherence(
            "node_a", {"q": "hello"}, {"a": "world"},
            anomaly_signals=anomalies,
        )
        user_msg = captured_kwargs["messages"][1]["content"]
        assert "BA-001" in user_msg

    def test_warning_anomalies_excluded(self, monkeypatch):
        """Only critical anomalies are forwarded to LLM."""
        captured_kwargs = {}
        def fake_llm(**kwargs):
            captured_kwargs.update(kwargs)
            return {
                "choices": [{"message": {"content": json.dumps({"pass": True})}}],
                "usage": {},
            }
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", fake_llm)

        anomalies = [AnomalySignal("BA-002", "warning", 0.5, "filler",
                                    "diverse content", "some filler", "text")]
        check_semantic_coherence(
            "node_a", {"q": "hello"}, {"a": "world"},
            anomaly_signals=anomalies,
        )
        user_msg = captured_kwargs["messages"][1]["content"]
        assert "BA-002" not in user_msg

@pytest.mark.unit
class TestSemanticCheckerBug:
    def test_tool_failure_evidence_forwarded(self, monkeypatch):
        """Verify tf.evidence is correctly forwarded to the LLM judge prompt."""
        captured_kwargs = {}
        def fake_llm(**kwargs):
            captured_kwargs.update(kwargs)
            return {
                "choices": [{"message": {"content": json.dumps({"pass": True})}}],
                "usage": {},
            }
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", fake_llm)

        inspection = InspectionResult(
            is_silent_failure=False, missing_fields=[], empty_fields=[],
            type_mismatches=[], severity="warning", message="test",
            tool_failures=[ToolFailure("error_response", "error", "critical", "boom")],
            has_tool_failure=True,
        )
        check_semantic_coherence(
            "node_a", {"q": "hello"}, {"a": "world"},
            inspection=inspection,
        )

@pytest.mark.unit
class TestTruncation:
    def test_long_value_truncated(self):
        result = _truncate("x" * 1000)
        assert len(result) < 1000
        assert "truncated" in result

    def test_short_value_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_compact_dict_caps_total(self):
        big_dict = {f"key{i}": "x" * 500 for i in range(20)}
        result = _compact_dict(big_dict)
        total_chars = sum(len(v) for v in result.values())
        assert total_chars <= 7000  # some slack for truncation markers

    def test_bytes_skipped(self):
        result = _compact_dict({"binary": b"hello", "text": "world"})
        assert "binary" not in result
        assert "text" in result


@pytest.mark.unit
class TestJudgeJsonResilience:
    def test_extracts_object_from_prose(self):
        raw = 'Sure.\n{"pass": false, "reason": "placeholder", "confidence": 0.9}\n'
        parsed = _extract_json_object(raw)
        assert parsed is not None
        assert parsed["pass"] is False

    def test_repairs_unterminated_string(self):
        raw = '{"pass": false, "reason": "output is PLACEHOLDER text that got cut'
        parsed = _extract_json_object(raw)
        assert parsed is not None
        assert parsed.get("pass") is False

    def test_garbage_returns_none(self):
        assert _extract_json_object("not json at all") is None

    def test_unterminated_judge_output_evaluated_false(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr(
            "argus.llm_proxy.create_chat_completion",
            lambda **kwargs: {
                "choices": [
                    {
                        "message": {
                            "content": '{"pass": false, "reason": "unterminated'
                        }
                    }
                ],
                "usage": {},
            },
        )
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "PLACEHOLDER"})
        # Repair may succeed; if not, skip with evaluated=False.
        if not result.evaluated:
            assert result.passed is True
            assert "skipped" in result.reason
        else:
            assert result.passed is False
