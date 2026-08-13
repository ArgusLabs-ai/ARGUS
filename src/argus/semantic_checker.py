"""Per-node semantic coherence check via a lightweight LLM call.

Evidence-aware judge: receives validator results, anomaly signals, and
inspection findings as context alongside input/output.  Returns an audit
trail (evidence_considered, overridden_signals) with every ruling.
Uses gpt-4o-mini by default — ~500 tokens in, ~100 tokens out.
"""

from __future__ import annotations

import json
import time
from typing import Any

from argus.models import SemanticCheckResult

_SYSTEM_PROMPT = (
    "You verify whether an AI pipeline node produced semantically correct "
    "output given its input. Respond with JSON:\n"
    '{"pass": bool, "reason": "<1 sentence>", "confidence": <0.0-1.0>, '
    '"evidence_considered": ["<signal1>", ...], '
    '"overridden_signals": ["<signal_you_disagree_with>", ...]}\n\n'
    "- evidence_considered: list every Prior Signal you evaluated (empty list if none provided)\n"
    "- overridden_signals: list any Prior Signals you chose to PASS despite "
    "(empty list if you agreed with all signals or none were provided)\n\n"
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
    "FAIL the node regardless of how reasonable the text looks."
)

_MAX_VALUE_LEN = 800
_MAX_PAYLOAD_CHARS = 6000


def _truncate(v: Any) -> str:
    """Convert a value to a truncated string representation."""
    s = str(v) if not isinstance(v, str) else v
    if len(s) > _MAX_VALUE_LEN:
        return s[:_MAX_VALUE_LEN] + "... (truncated)"
    return s


def _compact_dict(d: dict[str, Any]) -> dict[str, str]:
    """Produce a compact, truncated snapshot of a dict for the prompt."""
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


def _skip_result(reason: str, model: str, ms: float) -> SemanticCheckResult:
    """Return a passing result for skipped/errored checks.

    `evaluated=False` records that the judge never actually ran — distinct
    from a real judged pass, so callers that need to tell the two apart
    (coverage accounting, display) can.
    """
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
) -> SemanticCheckResult:
    """Check if a node's output is semantically coherent with its input.

    Returns a passing result on any error — this check must never block
    the pipeline.
    """
    t0 = time.perf_counter()

    from argus.llm_proxy import create_chat_completion, is_available  # noqa: PLC0415

    if not is_available():
        return _skip_result("check skipped: not logged in (run: argus login)", model, 0.0)

    compact_in = _compact_dict(input_state)
    compact_out = _compact_dict(output_dict)

    # Skip if there's nothing meaningful to compare
    if not compact_in or not compact_out:
        return _skip_result("check skipped: empty input or output", model, 0.0)

    user_msg = (
        f'Node: "{node_name}"\n'
        f"Input: {json.dumps(compact_in, default=str)}\n"
        f"Output: {json.dumps(compact_out, default=str)}"
    )

    # Inject prior evidence so the LLM judges with full context
    evidence_lines: list[str] = []

    failed_validators = [v for v in (validator_results or []) if not v.is_valid]
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

    try:
        result = create_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=150,
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=5.0,
        )
        elapsed = (time.perf_counter() - t0) * 1000

        if "error" in result:
            return _skip_result(f"check skipped: {result['error']}", model, elapsed)

        choices = result.get("choices", [])
        raw = choices[0]["message"]["content"] if choices else "{}"
        parsed = json.loads(raw)
        usage = result.get("usage", {})

        return SemanticCheckResult(
            passed=bool(parsed.get("pass", True)),
            reason=str(parsed.get("reason", "")),
            confidence=float(parsed.get("confidence", 0.0)),
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            duration_ms=round(elapsed, 2),
            evidence_considered=tuple(parsed.get("evidence_considered", ())),
            overridden_signals=tuple(parsed.get("overridden_signals", ())),
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _skip_result(f"check skipped: {exc}", model, elapsed)
