"""Per-node semantic coherence check via a lightweight LLM call.

Evidence-aware judge: receives validator results, anomaly signals,
inspection findings, AND ambiguous heuristic signals as context.
Returns a coherence ruling plus disambiguation verdicts — one LLM
call instead of two.

Uses gpt-4o-mini by default — ~600 tokens in, ~150 tokens out.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from argus.models import DisambiguationResult, SemanticCheckResult, SemanticSignal

_SYSTEM_PROMPT = (
    "You verify whether an AI pipeline node produced semantically correct "
    "output given its input. Respond with JSON:\n"
    '{"pass": bool, "reason": "<1 sentence>", "confidence": <0.0-1.0>, '
    '"evidence_considered": ["<signal1>", ...], '
    '"overridden_signals": ["<signal_you_disagree_with>", ...], '
    '"disambiguation_verdicts": [{"sig_id": "<id>", "is_failure": <bool>, '
    '"confidence": <0.0-1.0>, "reason": "<1 sentence>"}]}\n\n'
    "- evidence_considered: list every Prior Signal you evaluated (empty list if none provided)\n"
    "- overridden_signals: list any Prior Signals you chose to PASS despite "
    "(empty list if you agreed with all signals or none were provided)\n"
    "- disambiguation_verdicts: verdict for each Ambiguous Heuristic Match "
    "(empty list if none provided)\n\n"
    "Rules:\n"
    '- "pass": true if the output is a reasonable response to the input\n'
    '- "pass": false ONLY if the output is completely unrelated, contradictory, '
    "or nonsensical given the input\n"
    "- Do not judge quality or completeness, only semantic relevance\n"
    "- EXCEPTION: if a key output field is empty string, null, or blank while "
    "the input contained meaningful data for that field, FAIL the node — "
    "an empty output is not semantically relevant regardless of other fields "
    "like logs or metadata\n"
    "- If you cannot determine relevance (insufficient context), pass it\n"
    "- This is one node in a MULTI-STEP pipeline. The output does not need to "
    "directly answer the input — it may be an intermediate transformation "
    "(e.g. parsing, filtering, extracting, classifying). As long as the output "
    "is a plausible processing step on the input data, pass it.\n"
    "- Forwarding an intermediate pipeline field to the next node (copying "
    "`draft` to `reply`, `summary` to `answer`, and similar handoffs) is a "
    "legitimate passthrough, NOT input echo. Only fail echo when the node "
    "repeats the user prompt, query, or question as its answer.\n"
    "- The input/output shown may be TRUNCATED. Do NOT fail a node because "
    "the output references content that was truncated from the input. "
    "If a value ends with '... (truncated)' it was cut short — assume the "
    "full value contains more data than what you see.\n"
    "- When in doubt, PASS. False positives are worse than false negatives.\n"
    "- IMPORTANT: If 'Prior Signals' are provided, these are results from "
    "deterministic checks that ran before you. A validator failure means a "
    "specific business-logic constraint was violated (e.g., a required field "
    "is missing). You MUST weigh these heavily — if a validator flagged a "
    "missing required field and you can confirm it is absent from the output, "
    "FAIL the node regardless of how reasonable the text looks.\n"
    "- DISAMBIGUATION: If 'Ambiguous Heuristic Matches' are provided, these are "
    "pattern matches with borderline confidence. For each, determine if the matched "
    "pattern represents a real problem (placeholder text, corrupted output, semantic "
    "degradation) or legitimate content that happens to match. Return one verdict per "
    "match in disambiguation_verdicts. When in doubt, mark is_failure=true "
    "(false negatives are worse than false positives for disambiguation)."
)

_MAX_VALUE_LEN = 800
_MAX_PAYLOAD_CHARS = 6000


def _truncate(v: Any) -> str:
    s = str(v) if not isinstance(v, str) else v
    if len(s) > _MAX_VALUE_LEN:
        return s[:_MAX_VALUE_LEN] + "... (truncated)"
    return s


def _compact_dict(d: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    total = 0
    for k, v in d.items():
        if isinstance(v, (bytes, bytearray)):
            continue
        t = _truncate(v)
        total += len(t)
        if total > _MAX_PAYLOAD_CHARS:
            break
        out[k] = t
    return out


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a judge JSON object, repairing truncated / fenced payloads.

    Returns None if no object can be recovered — callers should fail closed
    to heuristics rather than treating the skip as a pass.
    """
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    def _as_dict(value: Any) -> dict[str, Any] | None:
        return value if isinstance(value, dict) else None

    try:
        parsed = _as_dict(json.loads(text))
        if parsed is not None:
            return parsed
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    if end > start:
        try:
            parsed = _as_dict(json.loads(text[start : end + 1]))
            if parsed is not None:
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return _repair_truncated_json(text[start:])


def _repair_truncated_json(fragment: str) -> dict[str, Any] | None:
    """Close an unterminated JSON object (truncated string / missing braces)."""
    s = fragment.rstrip()
    if not s.startswith("{"):
        return None
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in s:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()

    candidate = s
    if in_string:
        candidate += '"'
    candidate = candidate.rstrip().rstrip(",")
    candidate += "".join(reversed(stack))
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


_TRUE_STRINGS = frozenset({"true", "yes", "1"})
_FALSE_STRINGS = frozenset({"false", "no", "0"})


def _coerce_verdict(value: Any) -> bool | None:
    """Read a judge's boolean field, or None if it isn't a usable verdict.

    Accepts real booleans, the string forms models emit under JSON mode, and
    the 0/1 integers they substitute for booleans. Everything else — a missing
    key, null, a confidence-shaped float, a sentence — means the judge gave no
    verdict, and the caller must skip rather than invent one.

    `bool()` is the wrong tool here: bool("false") and bool("no") are both
    True, so a judge explicitly saying no would be recorded as a yes.

    0 and 1 are read rather than skipped because skipping loses a verdict the
    judge did give. Only those two integers qualify; anything else in that
    field is not a boolean the model got slightly wrong, and `1.0`/`0.9` are
    excluded by the isinstance check because a float there reads as a
    confidence that landed in the wrong key.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
    return None


def _skip_result(reason: str, model: str, ms: float) -> SemanticCheckResult:
    return SemanticCheckResult(
        passed=True,
        reason=reason,
        confidence=0.0,
        model=model,
        prompt_tokens=0,
        completion_tokens=0,
        duration_ms=round(ms, 2),
        evidence_considered=(),
        overridden_signals=(),
        evaluated=False,
    )


def check_semantic_coherence(
    node_name: str,
    input_state: dict[str, Any],
    output_dict: dict[str, Any],
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    *,
    validator_results: list[Any] | None = None,
    anomaly_signals: list[Any] | None = None,
    inspection: Any | None = None,
    ambiguous_signals: list[SemanticSignal] | None = None,
) -> tuple[SemanticCheckResult, list[DisambiguationResult]]:
    """Check coherence and disambiguate heuristic signals in one LLM call.

    Returns (coherence_result, disambiguation_results).
    On error returns a passing result and empty disambiguation list.
    """
    t0 = time.perf_counter()

    from argus.llm_proxy import create_chat_completion, is_available

    if not is_available():
        return _skip_result("check skipped: not logged in (run: argus login)", model, 0.0), []

    compact_in = _compact_dict(input_state)
    compact_out = _compact_dict(output_dict)

    if not compact_in or not compact_out:
        return _skip_result("check skipped: empty input or output", model, 0.0), []

    user_msg = (
        f'Node: "{node_name}"\n'
        f"Input: {json.dumps(compact_in, default=str)}\n"
        f"Output: {json.dumps(compact_out, default=str)}"
    )

    evidence_lines: list[str] = []

    failed_validators = [
        v
        for v in (validator_results or [])
        if not v.is_valid and getattr(v, "severity", "critical") != "warning"
    ]
    if failed_validators:
        evidence_lines.append("Validator failures:")
        for v in failed_validators:
            evidence_lines.append(f"  - [{v.validator_name}]: {v.message}")

    critical_anomalies = [a for a in (anomaly_signals or []) if a.severity == "critical"]
    if critical_anomalies:
        evidence_lines.append("Critical anomaly signals:")
        for a in critical_anomalies:
            evidence_lines.append(f"  - [{a.anomaly_id}] {a.reason}")

    if inspection:
        if inspection.missing_fields:
            evidence_lines.append(
                f"Missing required fields: {', '.join(inspection.missing_fields)}"
            )
        if inspection.tool_failures:
            evidence_lines.append("Tool failures:")
            for tf in inspection.tool_failures:
                evidence_lines.append(f"  - [{tf.failure_type}] {tf.evidence}")

    if evidence_lines:
        user_msg += (
            "\n\nPrior Signals (from deterministic checks that ran before you):\n"
            + "\n".join(evidence_lines)
            + "\n\nWeigh these signals heavily. A validator failure means a specific "
            "business-logic constraint was violated. A node can produce semantically "
            "relevant text while still violating structural/business constraints."
        )

    amb = ambiguous_signals or []
    if amb:
        flagged = [
            {
                "sig_id": s.sig_id,
                "category": s.category,
                "field_path": (
                    ".".join(s.field_path) if isinstance(s.field_path, tuple) else s.field_path
                ),
                "evidence": s.evidence,
                "description": s.description,
                "confidence": round(s.confidence, 3),
            }
            for s in amb
        ]
        user_msg += (
            "\n\nAmbiguous Heuristic Matches (need your verdict):\n"
            + json.dumps(flagged, indent=2)
            + "\n\nFor each match above, return a verdict in disambiguation_verdicts. "
            "is_failure=true means the pattern indicates a real problem. "
            "is_failure=false means the content is legitimate despite matching."
        )

    max_tokens = 150 if not amb else 200 + 30 * len(amb)

    try:
        result = create_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=8.0 if amb else 5.0,
        )
        elapsed = (time.perf_counter() - t0) * 1000

        if "error" in result:
            return _skip_result(f"check skipped: {result['error']}", model, elapsed), []

        choices = result.get("choices", [])
        if not choices:
            # No completion at all — the judge never ruled on anything.
            return _skip_result("check skipped: judge returned no completion", model, elapsed), []
        raw = choices[0]["message"]["content"]
        parsed = _extract_json_object(raw)
        if parsed is None:
            return _skip_result(
                "check skipped: Unterminated string / invalid JSON from judge",
                model,
                elapsed,
            ), []

        # A parseable body is not the same as a verdict. Defaulting a missing
        # or uninterpretable "pass" to True reports a fabricated ruling as a
        # real one (evaluated=True), which the caller may then act on.
        verdict = _coerce_verdict(parsed.get("pass"))
        if verdict is None:
            return _skip_result(
                "check skipped: judge response carried no pass/fail verdict",
                model,
                elapsed,
            ), []

        usage = result.get("usage", {})

        sc = SemanticCheckResult(
            passed=verdict,
            reason=str(parsed.get("reason", "")),
            confidence=float(parsed.get("confidence", 0.0)),
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            duration_ms=round(elapsed, 2),
            evidence_considered=tuple(parsed.get("evidence_considered", ())),
            overridden_signals=tuple(parsed.get("overridden_signals", ())),
        )

        dis_results: list[DisambiguationResult] = []
        if amb:
            sig_map = {s.sig_id: s for s in amb}
            for v in parsed.get("disambiguation_verdicts", []):
                sid = v.get("sig_id", "")
                if sid not in sig_map:
                    continue
                signal = sig_map[sid]
                # Same coercion trap as "pass" above. Here an unreadable
                # verdict keeps the prompt's documented default (treat as a
                # failure) instead of skipping the signal.
                is_failure = _coerce_verdict(v.get("is_failure"))
                dis_results.append(
                    DisambiguationResult(
                        sig_id=sid,
                        field_path=(
                            ".".join(signal.field_path)
                            if isinstance(signal.field_path, tuple)
                            else signal.field_path
                        ),
                        original_confidence=signal.confidence,
                        llm_verdict=True if is_failure is None else is_failure,
                        llm_confidence=float(v.get("confidence", 0.5)),
                        llm_reason=str(v.get("reason", "")),
                        model=model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        duration_ms=round(elapsed, 2),
                    )
                )

        return sc, dis_results
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _skip_result(f"check skipped: {exc}", model, elapsed), []
