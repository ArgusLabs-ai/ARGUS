"""Unit tests for argus.inspector — all 16 detection rules + structural inspection."""
import pytest
from conftest import make_event, make_inspection

from argus.inspector import build_root_cause_chain, inspect_tool_outputs, inspect_transition
from argus.models import SemanticSignal, ToolFailure

# ── Rule 1: Error keys ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestRule1ErrorKeys:
    def test_error_key_truthy(self):
        result = inspect_tool_outputs({"error": "something went wrong"})
        assert any(tf.failure_type == "error_response" for tf in result.tool_failures)
        assert result.has_tool_failure

    def test_error_key_falsy_skipped(self):
        result = inspect_tool_outputs({"error": ""})
        assert not any(tf.field_name == "error" for tf in result.tool_failures)

    def test_error_key_none_skipped(self):
        result = inspect_tool_outputs({"error": None})
        assert not any(tf.field_name == "error" for tf in result.tool_failures)

    def test_capitalized_error_key_not_caught(self):
        """Known gap: Rule 1 uses exact key matching, 'Error' won't match."""
        result = inspect_tool_outputs({"Error": "something broke"})
        assert not any(
            tf.field_name == "Error" and tf.failure_type == "error_response"
            for tf in result.tool_failures
        )

    def test_rate_limit_in_error_value(self):
        result = inspect_tool_outputs({"error": "rate limit exceeded"})
        assert any(tf.failure_type == "rate_limit" for tf in result.tool_failures)

    def test_error_key_with_list_value(self):
        result = inspect_tool_outputs({"errors": ["err1", "err2"]})
        assert any(tf.failure_type == "error_response" for tf in result.tool_failures)


# ── Rule 2: HTTP status codes ────────────────────────────────────────────────


@pytest.mark.unit
class TestRule2HttpStatus:
    def test_status_500(self):
        result = inspect_tool_outputs({"status_code": 500})
        assert any(
            tf.failure_type == "error_response" and "500" in tf.evidence
            for tf in result.tool_failures
        )

    def test_status_429_rate_limit(self):
        result = inspect_tool_outputs({"status_code": 429})
        assert any(tf.failure_type == "rate_limit" for tf in result.tool_failures)

    def test_status_200_no_failure(self):
        result = inspect_tool_outputs({"status_code": 200})
        assert not result.tool_failures

    def test_status_string_coerced(self):
        """Numeric string status codes must count as HTTP errors."""
        result = inspect_tool_outputs({"status_code": "500"})
        assert any(tf.failure_type == "error_response" for tf in result.tool_failures)
        assert result.has_tool_failure

    def test_status_non_numeric_string_ignored(self):
        result = inspect_tool_outputs({"status_code": "ok"})
        assert not any(tf.failure_type == "error_response" for tf in result.tool_failures)

    def test_status_400(self):
        result = inspect_tool_outputs({"status_code": 400})
        assert any(tf.failure_type == "error_response" for tf in result.tool_failures)


# ── Rule 2b: Boolean success=False ───────────────────────────────────────────


@pytest.mark.unit
class TestRule2bSuccessFalse:
    def test_success_false(self):
        result = inspect_tool_outputs({"success": False})
        assert result.has_tool_failure

    def test_success_true_no_failure(self):
        result = inspect_tool_outputs({"success": True})
        assert not any(tf.field_name == "success" for tf in result.tool_failures)

    def test_success_case_insensitive(self):
        """Rule 2b uses key.lower(), so 'Success' should match."""
        result = inspect_tool_outputs({"Success": False})
        assert result.has_tool_failure


# ── Rule 2c: Boolean failure=True ────────────────────────────────────────────


@pytest.mark.unit
class TestRule2cFailureTrue:
    def test_failed_true(self):
        result = inspect_tool_outputs({"failed": True})
        assert result.has_tool_failure

    def test_failed_false_no_failure(self):
        result = inspect_tool_outputs({"failed": False})
        assert not result.tool_failures


# ── Rule 3: Empty results ────────────────────────────────────────────────────


@pytest.mark.unit
class TestRule3EmptyResults:
    def test_empty_list(self):
        result = inspect_tool_outputs({"results": []})
        assert any(tf.failure_type == "empty_result" for tf in result.tool_failures)
        assert result.has_tool_failure

    def test_empty_documents_fails_node(self):
        result = inspect_tool_outputs({"documents": [], "query": "refund policy"})
        assert any(
            tf.failure_type == "empty_result" and tf.field_name == "documents"
            for tf in result.tool_failures
        )
        assert result.has_tool_failure
        assert result.severity == "critical"

    def test_empty_optional_string_not_promoted(self):
        """policy_notes='' is an optional blank, not a failed retrieval."""
        result = inspect_tool_outputs({"policy_notes": "", "answer": "Approved."})
        assert not any(
            tf.field_name == "policy_notes" and tf.severity == "critical"
            for tf in result.tool_failures
        )
        assert not result.has_tool_failure

    def test_none_result(self):
        result = inspect_tool_outputs({"data": None})
        assert any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    def test_empty_string_result(self):
        result = inspect_tool_outputs({"result": ""})
        assert any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    def test_non_empty_results_ok(self):
        result = inspect_tool_outputs({"results": [1, 2, 3]})
        assert not any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    # Hollow-collection cases: N results returned but every element is empty/null.
    # These look successful (non-zero length) but carry no content.
    def test_list_all_none(self):
        result = inspect_tool_outputs({"results": [None, None, None]})
        assert any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    def test_list_all_empty_dicts(self):
        result = inspect_tool_outputs({"documents": [{}, {}]})
        assert any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    def test_list_all_blank_strings(self):
        result = inspect_tool_outputs({"items": ["", "  ", ""]})
        assert any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    def test_dict_all_empty_values(self):
        result = inspect_tool_outputs({"data": {"a": None, "b": ""}})
        assert any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    def test_mixed_real_and_empty_ok(self):
        # Partial content present — not a hollow result, must stay clean.
        result = inspect_tool_outputs({"results": [None, {"title": "x"}]})
        assert not any(tf.failure_type == "empty_result" for tf in result.tool_failures)

    def test_numeric_zeros_ok(self):
        # 0 is valid data, not "empty" — must not flag.
        result = inspect_tool_outputs({"results": [0, 0, 0]})
        assert not any(tf.failure_type == "empty_result" for tf in result.tool_failures)


# ── Rule 4: Error strings ────────────────────────────────────────────────────


@pytest.mark.unit
class TestRule4ErrorStrings:
    def test_error_string_at_start(self):
        result = inspect_tool_outputs({"info": "error: connection refused"})
        assert any(tf.failure_type == "error_in_data" for tf in result.tool_failures)

    def test_timeout_string(self):
        result = inspect_tool_outputs({"info": "timeout exceeded"})
        assert any(tf.failure_type == "error_in_data" for tf in result.tool_failures)

    def test_mid_string_error_not_caught(self):
        """Known limitation: regex only matches at start of string."""
        result = inspect_tool_outputs({"info": "got an error from the API"})
        assert not any(tf.failure_type == "error_in_data" for tf in result.tool_failures)


# ── Rule 5: Partial failures ─────────────────────────────────────────────────


@pytest.mark.unit
class TestRule5PartialFailures:
    def test_partial_errors_in_list(self):
        result = inspect_tool_outputs(
            {
                "items": [
                    {"name": "a", "error": "timeout"},
                    {"name": "b"},
                    {"name": "c", "error": "rejected"},
                ]
            }
        )
        assert any(tf.failure_type == "partial_failure" for tf in result.tool_failures)
        tf = next(t for t in result.tool_failures if t.failure_type == "partial_failure")
        assert "2 of 3" in tf.evidence

    def test_falsy_error_not_counted(self):
        """Known gap: error=0 is falsy and won't be counted."""
        result = inspect_tool_outputs({"items": [{"error": 0}, {"error": False}]})
        assert not any(tf.failure_type == "partial_failure" for tf in result.tool_failures)


# ── Rule 6: Nested dicts ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestRule6NestedDicts:
    def test_nested_error_key(self):
        result = inspect_tool_outputs({"api_result": {"error": "failed"}})
        assert any(
            tf.field_name == "api_result.error" and tf.severity == "critical"
            for tf in result.tool_failures
        )
        assert result.has_tool_failure

    def test_nested_status_code(self):
        result = inspect_tool_outputs({"response": {"status_code": 500}})
        assert any(tf.field_name == "response.status_code" for tf in result.tool_failures)
        assert result.has_tool_failure

    def test_two_levels_deep_nested_error(self):
        result = inspect_tool_outputs({"outer": {"middle": {"error": "deep"}}})
        nested = [tf for tf in result.tool_failures if tf.field_name == "outer.middle.error"]
        assert nested
        assert nested[0].severity == "critical"
        assert result.has_tool_failure

    def test_three_levels_deep_success_false(self):
        result = inspect_tool_outputs(
            {"payload": {"tool": {"result": {"success": False}}}}
        )
        assert any(
            tf.field_name.endswith("success") and tf.severity == "critical"
            for tf in result.tool_failures
        )
        assert result.has_tool_failure

    def test_nested_string_status_code(self):
        result = inspect_tool_outputs({"response": {"body": {"status_code": "503"}}})
        assert any(
            tf.failure_type == "error_response" and "503" in tf.evidence
            for tf in result.tool_failures
        )
        assert result.has_tool_failure


# ── Rule 7: Semantic heuristics ──────────────────────────────────────────────


@pytest.mark.unit
class TestRule7SemanticHeuristics:
    def test_placeholder_detected(self):
        result = inspect_tool_outputs({"answer": "PLACEHOLDER"})
        has_semantic = any(
            tf.failure_type in ("placeholder_detected", "semantic_degradation")
            for tf in result.tool_failures
        )
        assert has_semantic or len(result.semantic_signals) > 0

    def test_confidence_below_03_skipped(self):
        """Signals with confidence < 0.3 should not create ToolFailures."""
        low_conf_signal = SemanticSignal(
            sig_id="TEST-001",
            category="test",
            severity="critical",
            description="test",
            field_path=("test",),
            evidence="test",
            confidence=0.2,
        )
        result = inspect_tool_outputs({"x": "y"}, _precomputed_signals=[low_conf_signal])
        assert not any(tf.evidence and "TEST-001" in tf.evidence for tf in result.tool_failures)

    def test_confidence_below_07_downgraded_to_warning(self):
        mid_conf_signal = SemanticSignal(
            sig_id="TEST-002",
            category="placeholder_outputs",
            severity="critical",
            description="test",
            field_path=("test",),
            evidence="test",
            confidence=0.5,
        )
        result = inspect_tool_outputs({"x": "y"}, _precomputed_signals=[mid_conf_signal])
        matching = [tf for tf in result.tool_failures if "TEST-002" in (tf.evidence or "")]
        assert matching
        assert matching[0].severity == "warning"

    def test_i_dont_know_signature_hit(self):
        result = inspect_tool_outputs({"answer": "I don't know."})
        assert result.semantic_signals or any(
            "SP-014" in (tf.evidence or "") or "don't know" in (tf.evidence or "").lower()
            for tf in result.tool_failures
        )


# ── Rule 8: Truncated output ─────────────────────────────────────────────────


@pytest.mark.unit
class TestRule8Truncation:
    def test_truncated_long_string(self):
        text = (
            "The quarterly earnings report showed a significant increase in "
            "revenue across all major business segments, with particularly "
            "strong growth observed in the technology division where "
            "new product launches contributed to higher than expected sa"
        )
        result = inspect_tool_outputs({"analysis": text})
        assert any(tf.failure_type == "truncated_output" for tf in result.tool_failures)
        # non-main field stays a warning
        trunc = next(tf for tf in result.tool_failures if tf.failure_type == "truncated_output")
        assert trunc.severity == "warning"

    def test_truncated_main_llm_field_is_critical(self):
        text = (
            "The quarterly earnings report showed a significant increase in "
            "revenue across all major business segments, with particularly "
            "strong growth observed in the technology division where "
            "new product launches contributed to higher than expected sa"
        )
        result = inspect_tool_outputs({"answer": text})
        trunc = [tf for tf in result.tool_failures if tf.failure_type == "truncated_output"]
        assert trunc
        assert trunc[0].severity == "critical"
        assert result.has_tool_failure

    def test_short_string_not_flagged(self):
        result = inspect_tool_outputs({"title": "Short ti"})
        assert not any(tf.failure_type == "truncated_output" for tf in result.tool_failures)

    def test_string_ending_with_period_not_flagged(self):
        text = "This is a complete sentence with proper ending." + " Extra words." * 20
        result = inspect_tool_outputs({"analysis": text})
        assert not any(tf.failure_type == "truncated_output" for tf in result.tool_failures)


# ── Rule 9: Hallucinated success ─────────────────────────────────────────────


@pytest.mark.unit
class TestRule9HallucinatedSuccess:
    def test_success_true_empty_results(self):
        result = inspect_tool_outputs({"success": True, "results": []})
        assert any(tf.failure_type == "confidence_mismatch" for tf in result.tool_failures)

    def test_success_true_nonempty_results(self):
        result = inspect_tool_outputs({"success": True, "results": [1, 2]})
        assert not any(
            tf.failure_type == "confidence_mismatch" and "success" in tf.field_name
            for tf in result.tool_failures
        )

    def test_status_ok_empty_data(self):
        result = inspect_tool_outputs({"status": "ok", "data": None})
        assert any(tf.failure_type == "confidence_mismatch" for tf in result.tool_failures)


# ── Rule 10: Confidence mismatch ─────────────────────────────────────────────


@pytest.mark.unit
class TestRule10ConfidenceMismatch:
    def test_high_confidence_with_hedging(self):
        result = inspect_tool_outputs(
            {
                "confidence": 0.95,
                "answer": "I'm not sure about this analysis",
            }
        )
        assert any(tf.failure_type == "confidence_mismatch" for tf in result.tool_failures)

    def test_low_confidence_no_flag(self):
        result = inspect_tool_outputs(
            {
                "confidence": 0.5,
                "answer": "I'm not sure about this",
            }
        )
        assert not any(
            tf.failure_type == "confidence_mismatch" and tf.field_name == "confidence"
            for tf in result.tool_failures
        )

    def test_string_confidence_not_caught(self):
        """Known gap: confidence as string is not caught (isinstance check for int/float)."""
        result = inspect_tool_outputs(
            {
                "confidence": "0.95",
                "answer": "I'm not sure about this",
            }
        )
        assert not any(
            tf.failure_type == "confidence_mismatch" and tf.field_name == "confidence"
            for tf in result.tool_failures
        )


# ── Rule 11: Retrieval quality ───────────────────────────────────────────────


@pytest.mark.unit
class TestRule11RetrievalQuality:
    def test_low_median_score(self):
        result = inspect_tool_outputs(
            {
                "results": [
                    {"score": 0.1, "text": "something"},
                    {"score": 0.2, "text": "another"},
                    {"score": 0.15, "text": "third"},
                ]
            }
        )
        assert any(tf.failure_type == "retrieval_quality_low" for tf in result.tool_failures)

    def test_good_scores_ok(self):
        result = inspect_tool_outputs(
            {
                "results": [
                    {"score": 0.8, "text": "something"},
                    {"score": 0.9, "text": "another"},
                ]
            }
        )
        assert not any(tf.failure_type == "retrieval_quality_low" for tf in result.tool_failures)

    def test_shallow_content(self):
        result = inspect_tool_outputs(
            {
                "results": [
                    {"score": 0.8, "content": "short"},
                    {"score": 0.9, "content": "tiny"},
                ]
            }
        )
        assert any(tf.failure_type == "shallow_context" for tf in result.tool_failures)


# ── Rule 12: Shallow summary ─────────────────────────────────────────────────


@pytest.mark.unit
class TestRule12ShallowSummary:
    def test_very_short_summary(self):
        result = inspect_tool_outputs({"summary": "ok"})
        assert any(tf.failure_type == "shallow_output" for tf in result.tool_failures)

    def test_short_summary_with_long_input(self):
        result = inspect_tool_outputs(
            {"summary": "This is a summary that is about ninety"
             " chars long, which is still short.  padding"},
            input_state={"document": "x" * 1000},
        )
        assert any(
            tf.failure_type == "information_compression_anomaly" for tf in result.tool_failures
        )


# ── Rule 13: Selective attention ─────────────────────────────────────────────


@pytest.mark.unit
class TestRule13SelectiveAttention:
    def test_list_reduced_over_50pct(self):
        result = inspect_tool_outputs(
            {"items": ["a", "b"]},
            input_state={"items": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]},
        )
        assert any(
            tf.failure_type == "selective_attention_reduction" for tf in result.tool_failures
        )

    def test_list_not_reduced_enough(self):
        result = inspect_tool_outputs(
            {"items": ["a", "b", "c", "d", "e", "f"]},
            input_state={"items": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]},
        )
        assert not any(
            tf.failure_type == "selective_attention_reduction" for tf in result.tool_failures
        )

    def test_small_input_list_skipped(self):
        result = inspect_tool_outputs(
            {"items": ["a"]},
            input_state={"items": ["a", "b", "c"]},
        )
        assert not any(
            tf.failure_type == "selective_attention_reduction" for tf in result.tool_failures
        )

    def test_reducer_field_skipped(self):
        result = inspect_tool_outputs(
            {"messages": ["new"]},
            input_state={"messages": ["a", "b", "c", "d", "e"]},
            reducer_fields={"messages": "operator.add"},
        )
        assert not any(
            tf.failure_type == "selective_attention_reduction" for tf in result.tool_failures
        )


# ── Rule 14: Input echo ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestRule14InputEcho:
    def test_echo_detected(self):
        text = (
            "This is a substantial piece of text that is long enough "
            "to trigger the echo detection rule" * 2
        )
        result = inspect_tool_outputs(
            {"output_text": text},
            input_state={"input_text": text},
        )
        assert any(tf.failure_type == "input_echo" for tf in result.tool_failures)

    def test_different_text_no_echo(self):
        result = inspect_tool_outputs(
            {"output_text": "Completely different output text " * 5},
            input_state={"input_text": "Original input about markets " * 5},
        )
        assert not any(tf.failure_type == "input_echo" for tf in result.tool_failures)

    def test_short_strings_skipped(self):
        result = inspect_tool_outputs(
            {"output": "short"},
            input_state={"input": "short"},
        )
        assert not any(tf.failure_type == "input_echo" for tf in result.tool_failures)

    def test_draft_to_reply_handoff_not_echo(self):
        draft = (
            "Here is the drafted customer reply covering the refund window, "
            "shipping exceptions, and the next steps we recommend they take. "
        ) * 2
        result = inspect_tool_outputs(
            {"reply": draft},
            input_state={"draft": draft, "query": "Please write a reply"},
        )
        assert not any(tf.failure_type == "input_echo" for tf in result.tool_failures)

    def test_query_echoed_as_answer_still_flagged(self):
        prompt = (
            "The market outlook is very bullish with strong momentum and "
            "positive indicators across every major sector this quarter. "
        ) * 2
        result = inspect_tool_outputs(
            {"answer": prompt},
            input_state={"query": prompt},
        )
        assert any(tf.failure_type == "input_echo" for tf in result.tool_failures)


# ── Rule 15: Contradictory transformation ────────────────────────────────────


@pytest.mark.unit
class TestRule15Contradiction:
    def test_bullish_bearish_contradiction(self):
        result = inspect_tool_outputs(
            {"analysis": "The market outlook is bearish with downside risk"},
            input_state={"query": "Provide a bullish analysis of the stock"},
        )
        assert any(tf.failure_type == "semantic_contradiction" for tf in result.tool_failures)

    def test_no_contradiction_same_sentiment(self):
        result = inspect_tool_outputs(
            {"analysis": "The market is bullish with upside potential"},
            input_state={"query": "Give me a bullish take on this stock"},
        )
        assert not any(tf.failure_type == "semantic_contradiction" for tf in result.tool_failures)


# ── Rule 16: Context overflow ────────────────────────────────────────────────


@pytest.mark.unit
class TestRule16ContextOverflow:
    def test_large_input_flagged(self):
        result = inspect_tool_outputs(
            {"summary": "result"},
            input_state={"document": "x" * 110_000},
        )
        assert any(tf.failure_type == "context_size_anomaly" for tf in result.tool_failures)

    def test_normal_input_ok(self):
        result = inspect_tool_outputs(
            {"summary": "result"},
            input_state={"document": "x" * 50_000},
        )
        assert not any(tf.failure_type == "context_size_anomaly" for tf in result.tool_failures)

    def test_truncated_sentinel_counts_as_large(self):
        result = inspect_tool_outputs(
            {"summary": "result"},
            input_state={
                "document": {"__argus_truncated__": True, "type": "str", "preview": "..."}
            },
        )
        assert any(tf.failure_type == "context_size_anomaly" for tf in result.tool_failures)



# ── Rule 17: Double-encoded JSON ─────────────────────────────────────────────


@pytest.mark.unit
class TestRule17JsonInString:
    def test_stringified_dict(self):
        result = inspect_tool_outputs({"data": '{"key": "val"}'}, _precomputed_signals=[])
        assert any(tf.failure_type == "json_in_string" for tf in result.tool_failures)
        assert any(tf.severity == "warning" for tf in result.tool_failures if tf.failure_type == "json_in_string")

    def test_stringified_list(self):
        result = inspect_tool_outputs({"data": '[1, 2, 3]'}, _precomputed_signals=[])
        assert any(tf.failure_type == "json_in_string" for tf in result.tool_failures)

    def test_expected_skipped_fields(self):
        result = inspect_tool_outputs({
            "raw_response": '{"key": "val"}',
            "log": '[1, 2, 3]',
            "logs": '{"a": 1}',
            "raw": '{"x": 2}',
            "history": '[]',
            "raw_output": '{}',
            "payload": '{"key": "val"}',
            "PAYLOAD": '{"key": "val"}'
        }, _precomputed_signals=[])
        assert not any(tf.failure_type == "json_in_string" for tf in result.tool_failures)

    def test_nested_structure(self):
        result = inspect_tool_outputs({"outer": {"inner": '{"key": "val"}'}}, _precomputed_signals=[])
        assert any(tf.failure_type == "json_in_string" and tf.field_name == "outer.inner" for tf in result.tool_failures)

        result2 = inspect_tool_outputs({"outer_list": [{'nested': '{"key": "val"}'}]}, _precomputed_signals=[])
        assert any(tf.failure_type == "json_in_string" and tf.field_name == "outer_list[0].nested" for tf in result2.tool_failures)

    def test_non_json_string_ignored(self):
        result = inspect_tool_outputs({"data": "just a normal string"}, _precomputed_signals=[])
        assert not any(tf.failure_type == "json_in_string" for tf in result.tool_failures)

    def test_malformed_json_ignored(self):
        result = inspect_tool_outputs({"data": '{"key": "value"'}, _precomputed_signals=[])
        assert not any(tf.failure_type == "json_in_string" for tf in result.tool_failures)
# ── Strict mode ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestStrictMode:
    def test_strict_promotes_warnings_to_critical(self):
        result = inspect_tool_outputs({"results": []}, strict=True)
        assert all(tf.severity == "critical" for tf in result.tool_failures)


# ── inspect_transition structural checks ─────────────────────────────────────


@pytest.mark.unit
class TestStructuralInspection:
    def test_none_output_no_crash(self):
        result = inspect_transition("node_a", None, {}, [])
        assert result.severity == "ok"
        assert "crashed" in result.message.lower()

    def test_no_successors_tool_failures_only(self):
        result = inspect_transition("node_a", {"error": "boom"}, {}, [])
        assert result.has_tool_failure

    def test_missing_field_detected_when_dropped(self):
        from typing import TypedDict

        class NextState(TypedDict):
            summary: str
            score: int

        def successor(state: NextState) -> dict:
            return state

        result = inspect_transition(
            "node_a",
            {"other_key": "value"},
            {"other_key": "value"},
            [successor],
            input_state={"summary": "kept", "score": 1},
        )
        # Dropped from input → missing. Unrelated extra keys do not invent this.
        assert "summary" in result.missing_fields or "score" in result.missing_fields

    def test_unknown_key_does_not_match_unrelated_schema_field(self):
        from typing import TypedDict

        class NextState(TypedDict, total=False):
            attempts: int
            query: str

        def successor(state: NextState) -> dict:
            return state

        result = inspect_transition(
            "node_a",
            {"status_code": 200, "query": "hello"},
            {"status_code": 200, "query": "hello"},
            [successor],
            input_state={"query": "hello"},
        )
        assert "attempts" not in result.missing_fields


# ── build_root_cause_chain ───────────────────────────────────────────────────


@pytest.mark.unit
class TestRootCauseChain:
    def test_crash_traces_to_upstream(self):
        events = [
            make_event("node_a", "pass", output={"query": "test"}, step_index=0),
            make_event(
                "node_b",
                "crashed",
                output=None,
                step_index=1,
                exception="Traceback:\nKeyError: 'summary'",
            ),
        ]
        chain = build_root_cause_chain(events, edge_map={"node_a": ["node_b"]})
        assert "node_a" in chain

    def test_clean_run_empty_chain(self):
        events = [
            make_event("node_a", "pass", output={"x": 1}, step_index=0),
            make_event("node_b", "pass", output={"y": 2}, step_index=1),
        ]
        chain = build_root_cause_chain(events)
        assert chain == []

    def test_degraded_input_skipped(self):
        events = [
            make_event(
                "node_a",
                "fail",
                output={"x": 1},
                step_index=0,
                inspection=make_inspection(
                    tool_failures=[
                        ToolFailure("error_response", "error", "critical", "boom")
                    ],
                    has_tool_failure=True,
                ),
            ),
            make_event("node_b", "degraded_input", output={"y": 2}, step_index=1),
        ]
        chain = build_root_cause_chain(events)
        assert "node_b" not in chain
        assert "node_a" in chain

    def test_semantic_fail_in_chain(self):
        events = [
            make_event(
                "node_a", "semantic_fail", output={"x": "placeholder"}, step_index=0
            ),
        ]
        chain = build_root_cause_chain(events)
        assert "node_a" in chain

    def test_silent_field_drop_blames_retrieve_not_synthesize(self):
        """LangGraph: retrieve omits documents; synthesize KeyErrors on it."""
        events = [
            make_event(
                "retrieve",
                "pass",
                output={"query": "refund policy", "corpus_id": "kb-1"},
                input_state={"query": "refund policy"},
                step_index=0,
            ),
            make_event(
                "synthesize",
                "crashed",
                output=None,
                step_index=1,
                exception=(
                    "KeyError: 'documents'\n"
                    "Traceback (most recent call last):\n"
                    "KeyError: 'documents'"
                ),
            ),
        ]
        chain = build_root_cause_chain(
            events,
            edge_map={"retrieve": ["synthesize"]},
        )
        assert "retrieve" in chain
        assert "synthesize" not in chain

    def test_silent_field_drop_walks_past_passthrough(self):
        """retrieve omits documents; rerank passthrough; synthesize KeyErrors."""
        events = [
            make_event(
                "retrieve",
                "pass",
                output={"query": "q", "retrieved_docs": ["a"]},
                input_state={"query": "q"},
                step_index=0,
            ),
            make_event(
                "rerank",
                "pass",
                output={"query": "q", "retrieved_docs": ["a"]},
                input_state={"query": "q", "retrieved_docs": ["a"]},
                step_index=1,
            ),
            make_event(
                "synthesize",
                "crashed",
                output=None,
                step_index=2,
                exception="KeyError('documents')",
            ),
        ]
        chain = build_root_cause_chain(
            events,
            edge_map={"retrieve": ["rerank"], "rerank": ["synthesize"]},
        )
        assert "retrieve" in chain
        assert "synthesize" not in chain
        assert "rerank" not in chain


# ── Typo detection via edit distance ────────────────────────────────────────


@pytest.mark.unit
class TestTypoDetection:
    def test_edit_distance_basic(self):
        from argus.inspector import _edit_distance

        assert _edit_distance("summary", "sumary") <= 2
        assert _edit_distance("summary", "summary") == 0
        assert _edit_distance("abc", "xyz") == 3

    def test_is_likely_typo_match(self):
        from argus.inspector import _is_likely_typo

        assert _is_likely_typo(["sumary"], "summary") is True
        assert _is_likely_typo(["scoree"], "score") is True

    def test_is_likely_typo_no_match(self):
        from argus.inspector import _is_likely_typo

        assert _is_likely_typo(["completely_different"], "summary") is False

    def test_is_likely_typo_length_guard(self):
        from argus.inspector import _is_likely_typo

        assert _is_likely_typo(["a"], "very_long_field_name") is False

    def test_typo_detected_in_structural_inspection(self):
        from typing import TypedDict

        class NextState(TypedDict):
            summary: str

        def successor(state: NextState) -> dict:
            return state

        result = inspect_transition(
            "node_a",
            {"sumary": "typo field"},
            {"sumary": "typo field"},
            [successor],
        )
        assert result.missing_fields or result.message


# ── TypedDict field introspection ───────────────────────────────────────────


@pytest.mark.unit
class TestTypedDictIntrospection:
    def test_typeddict_fields_checked(self):
        from typing import TypedDict

        class ExpectedOutput(TypedDict):
            answer: str
            confidence: float

        def successor(state: ExpectedOutput) -> dict:
            return state

        result = inspect_transition(
            "node_a",
            {"answer": "hello", "confidence": 0.9},
            {"answer": "hello", "confidence": 0.9},
            [successor],
        )
        assert result.severity == "ok"

    def test_unrelated_extra_key_does_not_invent_missing_fields(self):
        from typing import TypedDict

        class ExpectedOutput(TypedDict):
            answer: str
            confidence: float

        def successor(state: ExpectedOutput) -> dict:
            return state

        result = inspect_transition(
            "node_a",
            {"wrong_key": "value"},
            {"wrong_key": "value"},
            [successor],
        )
        # Unrelated extra keys must not be fuzzy-matched onto answer/confidence.
        assert "answer" not in result.missing_fields
        assert "confidence" not in result.missing_fields

    def test_typeddict_type_mismatch_flagged(self):
        from typing import TypedDict

        class ExpectedOutput(TypedDict):
            score: int

        def successor(state: ExpectedOutput) -> dict:
            return state

        result = inspect_transition(
            "node_a",
            {"score": "not_an_int"},
            {"score": "not_an_int"},
            [successor],
        )
        assert len(result.type_mismatches) >= 1

    def test_check_fields_with_node_provided_keys(self):
        from argus.inspector import _check_fields

        expected = {
            "summary": {"required": True, "type": str},
            "score": {"required": True, "type": int},
        }
        actual = {"summary": "hello"}
        missing, empty, mismatches = _check_fields(
            expected, actual, node_provided_keys={"summary"},
        )
        assert "score" not in missing
