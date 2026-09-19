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
from dataclasses import replace
from typing import Any

from argus.models import DisambiguationResult, SemanticCheckResult, SemanticSignal

_SYSTEM_PROMPT = (
    "You verify whether an AI pipeline node produced semantically correct "
    "output given its input. Respond with JSON:\n"
    '{"pass": bool, "reason": "<1 sentence>", "confidence": <0.0-1.0>, '
    '"failure_kind": "unrelated"|"contradiction"|"empty_or_missing"|"other", '
    '"evidence_considered": ["<signal1>", ...], '
    '"overridden_signals": ["<signal_you_disagree_with>", ...], '
    '"disambiguation_verdicts": [{"sig_id": "<id>", "is_failure": <bool>, '
    '"confidence": <0.0-1.0>, "reason": "<1 sentence>"}]}\n\n'
    '- failure_kind: when "pass" is false, say which kind of failure it is.\n'
    '    "unrelated" — the output is about a DIFFERENT SUBJECT than the input. '
    "This is about subject matter only. Cake ingredients in, helicopter rotors "
    "out: unrelated. A node that stays on the input's subject but does not "
    "answer the question, covers only one aspect of it, or contributes a single "
    "fact to a shared list is NOT unrelated — that is a normal step in a "
    'pipeline and must be "pass": true. Ask "is this the same topic?", '
    'never "does this answer the question?".\n'
    "    A short output that is a LABEL rather than prose — a verdict "
    "(APPROVE/REVISE), a category, a routing key, a score, a count, a boolean, "
    'a status — is a classification result. It is never "unrelated", however '
    "little it resembles the input text.\n"
    '    "contradiction" — the output asserts the opposite of the input or of '
    "itself (a summary saying a recipe has no eggs when the input lists eggs). "
    "A field that appears in BOTH Input and Output is being UPDATED by this "
    "node: a list that was empty and now has items, a count that changed, a "
    "status that moved on, a flag that flipped — that is the node's work and is "
    'NEVER a contradiction. "contradiction" is only about output text versus '
    "input facts the node did not write.\n"
    '    "empty_or_missing" — a field is blank, null or absent.\n'
    '    "other" — anything else. Use "other" when "pass" is true.\n'
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
    "- That exception applies to the OUTPUT ONLY. NEVER fail a node because a "
    "field in the INPUT is empty, missing or blank — the input is context you "
    "are given, not the node's work. In particular, a message history often "
    "contains assistant turns whose 'content' is empty because they carried a "
    "tool call instead; that is normal and is never a reason to fail.\n"
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
    "- EARLIER STEPS: If an 'Earlier steps' section is provided, it is the run's "
    "history for the fields this node cares about — what each prior step wrote "
    "and whether the field was still populated after it. Use it only to explain "
    "an empty or missing value in THIS node's input or output: if a field this "
    "node needed was populated earlier and an earlier step emptied or dropped "
    'it, fail with failure_kind "empty_or_missing" and name that step in the '
    "reason. Never fail a node for what an earlier step did to a field this "
    "node neither reads nor writes, and never fail a node for a change it made "
    "itself — consuming a queue, filtering a list or replacing a value it "
    "writes is the node doing its job.\n"
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


_FAILURE_KINDS = frozenset({"unrelated", "contradiction", "empty_or_missing", "other"})


def _coerce_failure_kind(value: Any) -> str:
    """Normalise the judge's `failure_kind`, defaulting to the cautious answer.

    An unknown or absent value becomes ``"other"``, which needs a corroborating
    rule finding before it can fail a build — so a malformed reply degrades to
    "annotate only" rather than to a standalone gate failure.
    """
    if isinstance(value, str) and value.strip().lower() in _FAILURE_KINDS:
        return value.strip().lower()
    return "other"


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


def _is_tool_call_turn(output_dict: dict[str, Any]) -> bool:
    """Is this update a model turn that only issued tool calls?

    A tool-calling turn puts its payload in ``tool_calls`` and leaves
    ``content`` empty. There is no prose to rule on, and asked anyway the judge
    reliably answers "the content field is empty, so the output is not
    semantically relevant" — at full confidence, on every agent turn, which is
    enough on its own to fail a working ``create_react_agent``. Judging a
    function call as if it were an answer is a category error, so skip it.
    """
    messages = output_dict.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    return all(
        isinstance(m, dict) and bool(m.get("tool_calls")) and _is_blank(m.get("content"))
        for m in messages
    )


def _tracked_fields(
    node_name: str,
    consumers: dict[str, Any] | None,
    input_state: dict[str, Any],
    output_dict: dict[str, Any],
) -> set[str]:
    """Which fields this node's *history* can legitimately turn on.

    What the node reads — its input keys, plus every field the consumer map
    declares it reads. Wider than that is every field of every prior row: the
    trace-size blow-up #85 rules out, and evidence about fields the node never
    touches is what makes a judge invent failures.

    Fields the node **writes** are removed, even when it also reads them. The
    node's update is the authority on their current value and is already in the
    prompt; adding "this used to be fuller" turns every legitimate consumption
    into an accusation. Measured, not theorised: on a supervisor loop draining a
    work queue (`{"pending": [...]}` → `[]` as workers consume it), showing the
    writer its own field's history failed the worker on 6 of 6 live runs, a
    pipeline the blind judge passed. A node that empties what it writes is doing
    its job; a node that *reads* a field someone else emptied is #85's case, and
    that is the one this keeps.
    """
    declared = {f for f, spec in (consumers or {}).items() if node_name in _readers_of(spec)}
    return (declared | set(input_state)) - set(output_dict)


def _readers_of(spec: Any) -> tuple[str, ...]:
    """Readers from either consumer-map shape: a list, or a dict with ``readers``."""
    if isinstance(spec, dict):
        return tuple(spec.get("readers") or ())
    return tuple(spec or ())


def _history_lines(
    prior_rows: list[Any],
    fields: set[str],
) -> list[str]:
    """One line per prior step that wrote a tracked field.

    ``prior_rows`` are :class:`argus.ledger.LedgerRow`s for the steps *before*
    this one. A step that wrote nothing relevant is not a line: the judge needs
    the field's history, not the run's.
    """
    lines: list[str] = []
    for row in prior_rows:
        update = row.update if isinstance(row.update, dict) else {}
        wrote = {k: v for k, v in update.items() if k in fields}
        if not wrote:
            continue
        for key, value in wrote.items():
            state = "now empty" if _is_blank(value) else "populated"
            lines.append(f'  - "{row.node}" wrote {key} = {_truncate(value)} ({state})')
    return lines


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _confirm_standalone_coherence(
    sc: SemanticCheckResult,
    node_name: str,
    user_msg: str,
    model: str,
    api_key: str | None,
) -> SemanticCheckResult:
    """Ask a second time before letting a coherence verdict fail a build alone.

    An "unrelated" / "contradiction" verdict is the only kind that gates CI with
    no rule agreeing (see `models.JUDGE_STANDALONE_FAILURE_KINDS`), because no
    deterministic rule can see that a node fed cake ingredients wrote about
    helicopters. That privilege makes its false positives expensive, and a
    single sample from a model is not stable: the same healthy fan-out branch
    came back "unrelated" on roughly one run in eight.

    Two independent samples have to agree. A genuine mismatch of subject is
    obvious enough to reproduce; an intermittent misread is not. If the second
    look disagrees, the verdict is kept and reported but demoted to "other", so
    it now needs a corroborating rule finding like any other judge opinion.

    The second look is a *different question*, not the first prompt replayed.
    Replaying the same prompt at temperature 0 reproduced the same misread
    almost every time — the model was asked to check its own work with its own
    eyes. Here it is handed the first verdict as a claim to audit, together
    with the false-alarm shapes this rule exists to catch, and asked whether
    the claim holds. Measured: a lint node appending to a reducer
    (`findings: []` → `[F401]`) was "contradiction" at 0.9 on one run in three
    and the replay agreed with itself every time.

    Costs one extra short call, and only on the rare path where a run is about
    to fail on the judge's word alone.
    """
    from argus.models import JUDGE_STANDALONE_FAILURE_KINDS

    if sc.passed or sc.failure_kind not in JUDGE_STANDALONE_FAILURE_KINDS:
        return sc

    from argus.llm_proxy import create_chat_completion

    audit = (
        f'A first reviewer failed node "{node_name}" as "{sc.failure_kind}" with the '
        f"reason: {sc.reason}\n\n"
        "Audit that verdict against the node below. Uphold it only if the output "
        "is genuinely about a different subject than the input, or genuinely "
        "asserts the opposite of an input fact the node did not itself write. "
        "Common false alarms that must be overturned: a field present in both "
        "input and output is being updated by this node (an empty list gaining "
        "items, a status moving on, a count changing) — that is its work, not a "
        "contradiction; a label, verdict, score, count, boolean or routing key "
        "is a classification result and is never unrelated; a step that covers "
        "one aspect of the input or adds one fact to a shared list is a normal "
        "pipeline step; an empty list a scanner legitimately returned is not a "
        "contradiction.\n\n" + user_msg
    )

    try:
        second = create_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": audit},
            ],
            temperature=0,
            max_tokens=200,
            api_key=api_key,
        )
        parsed = _extract_json_object(
            second.get("choices", [{}])[0].get("message", {}).get("content", "")
        )
    except Exception:
        parsed = None

    if parsed is None:
        return sc  # could not get a second opinion — leave the first as it is

    agrees = (
        _coerce_verdict(parsed.get("pass")) is False
        and _coerce_failure_kind(parsed.get("failure_kind")) in JUDGE_STANDALONE_FAILURE_KINDS
    )
    if agrees:
        return sc

    return replace(
        sc,
        failure_kind="other",
        reason=f"{sc.reason} (not reproduced on a second look — needs a rule to agree)",
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
    prior_rows: list[Any] | None = None,
    consumers: dict[str, Any] | None = None,
) -> tuple[SemanticCheckResult, list[DisambiguationResult]]:
    """Check coherence and disambiguate heuristic signals in one LLM call.

    Returns (coherence_result, disambiguation_results).
    On error returns a passing result and empty disambiguation list.

    ``prior_rows`` are the ledger rows for the steps *before* this node (#85).
    Without them the judge sees one node's I/O and is structurally blind to the
    commonest silent failure in a shared-state graph: a field written early,
    emptied legitimately partway through, still needed here. Scoped to the
    fields this node reads or writes (``consumers`` declares the rest), so the
    prompt grows with the node's contract, not with the run.
    """
    t0 = time.perf_counter()

    from argus.llm_proxy import create_chat_completion, is_available

    if not is_available():
        return _skip_result("check skipped: not logged in (run: argus login)", model, 0.0), []

    compact_in = _compact_dict(input_state)
    compact_out = _compact_dict(output_dict)

    if not compact_in or not compact_out:
        return _skip_result("check skipped: empty input or output", model, 0.0), []

    if _is_tool_call_turn(output_dict):
        return _skip_result("check skipped: tool-call turn, no prose to judge", model, 0.0), []

    user_msg = (
        f'Node: "{node_name}"\n'
        f"Input: {json.dumps(compact_in, default=str)}\n"
        f"Output: {json.dumps(compact_out, default=str)}"
    )

    history = _history_lines(
        prior_rows or [],
        _tracked_fields(node_name, consumers, input_state, output_dict),
    )
    if history:
        user_msg += "\n\nEarlier steps (the run so far, fields this node reads or writes):\n" + (
            "\n".join(history)
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
            failure_kind=_coerce_failure_kind(parsed.get("failure_kind")),
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

        sc = _confirm_standalone_coherence(sc, node_name, user_msg, model, api_key)

        return sc, dis_results
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return _skip_result(f"check skipped: {exc}", model, elapsed), []
