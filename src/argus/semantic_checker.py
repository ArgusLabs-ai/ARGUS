"""Per-node semantic coherence check via a lightweight LLM call.

Evidence-aware judge: receives validator results, anomaly signals,
inspection findings, AND ambiguous heuristic signals as context.
Returns a coherence ruling plus disambiguation verdicts — one LLM
call instead of two.

The verdict travels a forced tool-call channel (record_verdict): the model
must emit its ruling as function arguments, so no prose-wrapping, truncation,
or boolean-as-string repair stands between the ruling and the parser. The
free-text JSON path is kept as a one-shot fallback for providers that drop
the tools, and every skip still lands on heuristics (evaluated=False).

Uses gpt-4o-mini by default — ~600 tokens in, ~150 tokens out.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from argus import determinism, verdict_cache
from argus.models import DisambiguationResult, SemanticCheckResult, SemanticSignal

_SYSTEM_PROMPT = (
    "You review the flags a deterministic checker already raised on one node "
    "in an AI pipeline. You do not hunt for new failures. Record your verdict "
    "by calling the record_verdict tool — its schema mirrors this contract. "
    "If no tool is available to you, respond with JSON instead:\n"
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
    '- "pass": true if the Prior Signals are wrong or the output is a reasonable '
    "response to the input (a false-positive flag: customer text that reads like "
    "a refusal, an empty list that is a real LGTM, a field the node is updating)\n"
    '- "pass": false if those Prior Signals are correct — keep them. Do not invent '
    "a new failure the signals did not name\n"
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
        parsed = _as_dict(_loads_verdict(text))
        if parsed is not None:
            return parsed
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass

    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    if end > start:
        try:
            parsed = _as_dict(_loads_verdict(text[start : end + 1]))
            if parsed is not None:
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            pass

    return _repair_truncated_json(text[start:])


# The keys that carry a judge's ruling (a boolean ``pass``, or a choice in
# ``verdict``). Everything else in a verdict is explanation.
_DECISIVE_KEYS = frozenset({"pass", "verdict"})


def _no_repeated_ruling(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook``: an object that names a decisive key twice has no ruling.

    ``json.loads`` keeps the first key's position but the *last* value, so
    ``{"verdict": "clean", "reason": "x", "verdict": "clean_with_wa`` would
    look like a payload whose last key is ``reason``.

    The hook runs on every object at every depth, so a ``pass`` or ``verdict``
    repeated anywhere (inside one entry of a ``verdicts`` list, say) fails the
    whole payload closed. Deliberately simple: a model that repeats a ruling
    key is not to be trusted with the rest of the payload either.
    """
    keys = [key for key, _ in pairs]
    if any(keys.count(key) > 1 for key in _DECISIVE_KEYS):
        raise ValueError("repeated ruling key")
    return dict(pairs)


def _loads_verdict(text: str) -> Any:
    """``json.loads`` for every place a verdict is read, so they all agree.

    ``strict=False`` lets a raw CR/LF sit inside a string (models do emit one
    in ``reason``); strict JSON would drop the whole verdict for it. The hook
    makes a ruling named twice a ``ValueError``, whole payload or cut one.
    """
    return json.loads(text, strict=False, object_pairs_hook=_no_repeated_ruling)


def _repair_truncated_json(fragment: str) -> dict[str, Any] | None:
    """Close an unterminated JSON object (truncated string / missing braces).

    Closing a cut-off *explanation* is fine. A cut-off *ruling* is not: a
    ``"verdict": "clean`` closes to ``clean`` even when the model was writing
    ``clean_with_warnings``, which is a different score. A ruling must arrive
    whole (its string closed, its boolean spelled out), and appear once, or
    there is no repair.

    Covered: a cut inside a string value, or right after a value that is
    already whole. Not covered, and giving no verdict (as before this guard): a
    cut right after a backslash or inside a ``\\uXXXX`` escape, inside a key
    name, right after a colon, or inside a half-written literal (``fal``,
    ``0.``), including one nested in ``disambiguation_verdicts``.

    The ruling can sit at any depth (a list of per-clause ``verdict`` values is
    read item by item), so the check follows the cut to the last value in the
    payload and the key that owns it, whichever object it is in. A cut ``no``
    would close to ``no`` and read as ``False`` while the model was writing
    ``none``. A decisive value nested deeper that is whole, or a cut in any
    other key, is repaired as before.
    """
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
        parsed = _loads_verdict(candidate)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    # Only the last value can be the cut one. A closed string or a spelled-out
    # true/false is whole; an open string or a bare number (`1` of `10`) is not.
    key, value = _last_value(parsed)
    if key in _DECISIVE_KEYS and (in_string or not isinstance(value, (bool, str))):
        return None
    return parsed


def _last_value(parsed: dict[str, Any]) -> tuple[str | None, Any]:
    """The last value in a parsed payload, at any depth, and the key that owns it.

    A list's items belong to the key that holds the list. The descent stops at
    a decisive key: whatever it holds is the ruling.
    """
    key: str | None = None
    node: Any = parsed
    while True:
        if isinstance(node, dict) and node:
            key = next(reversed(node))
            node = node[key]
            if key in _DECISIVE_KEYS:
                return key, node
        elif isinstance(node, list) and node:
            node = node[-1]
        else:
            return key, node


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


def build_verdict_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """One OpenAI-shaped function tool from a JSON-schema property map.

    Any LLM verdict in ARGUS can travel the same forced-tool-call channel.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


JUDGE_VERDICT_TOOL = build_verdict_tool(
    "record_verdict",
    (
        "Record the coherence verdict for the node under review. Call this "
        "exactly once, with every field filled in."
    ),
    {
        "pass": {
            "type": "boolean",
            "description": (
                "true if the Prior Signals are wrong or the output is a "
                "reasonable response to the input; false if those signals are "
                "correct and should be kept."
            ),
        },
        "reason": {
            "type": "string",
            "maxLength": 200,
            "description": "One sentence: why it passes or fails.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "How confident you are in the verdict, 0.0-1.0.",
        },
        "failure_kind": {
            "type": "string",
            "enum": sorted(_FAILURE_KINDS),
            "description": (
                "When pass is false, which kind of failure: unrelated (output "
                "is about a different subject than the input), contradiction "
                "(output asserts the opposite of the input or itself), "
                "empty_or_missing (a field is blank, null or absent), other. "
                "Use other when pass is true."
            ),
        },
        "evidence_considered": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every Prior Signal you evaluated (empty if none).",
        },
        "overridden_signals": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Prior Signals you chose to PASS despite (empty if you agreed "
                "with all or none were provided)."
            ),
        },
        "disambiguation_verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sig_id": {"type": "string"},
                    "is_failure": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reason": {"type": "string", "maxLength": 200},
                },
                "required": ["sig_id", "is_failure", "confidence", "reason"],
                "additionalProperties": False,
            },
            "description": (
                "One verdict per Ambiguous Heuristic Match: is_failure true "
                "means the pattern is a real problem, false means legitimate "
                "content that happens to match."
            ),
        },
    },
    ["pass", "reason", "confidence", "failure_kind"],
)

# Forced function call: the verdict must arrive as tool arguments, not prose.
JUDGE_TOOL_CHOICE = {"type": "function", "function": {"name": "record_verdict"}}


def _verdict_from_tool_calls(result: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the first tool call's arguments out of a normalized completion.

    Every BYOK adapter (providers.py) hands the arguments over as a JSON
    string; a dict is also tolerated for hand-rolled test doubles. Arguments cut
    off by ``max_tokens`` get the same truncation repair as the content path,
    which refuses a ruling cut mid-value. Anything still unparseable gave no
    verdict, and the caller falls back to the free-text content path exactly
    once, then skips.
    """
    choices = result.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return None
    arguments = (tool_calls[0].get("function") or {}).get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return None
    try:
        parsed = _loads_verdict(arguments)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return _repair_truncated_json(arguments.lstrip())
    return parsed if isinstance(parsed, dict) else None


def _parse_verdict_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Verdict payload from a completion, tool-call first, free-text fallback.

    The structured channel is tried first; a response with no tool_calls
    (older mocks, providers that dropped the tools, the hosted proxy) is read
    as free text through the repairing extractor — one fallback, then the
    caller skips.
    """
    parsed = _verdict_from_tool_calls(result)
    if parsed is not None:
        return parsed
    choices = result.get("choices") or []
    if not choices:
        return None
    raw = (choices[0].get("message") or {}).get("content")
    if not isinstance(raw, str):
        return None
    return _extract_json_object(raw)


def reply_is_clean(result: dict[str, Any]) -> bool:
    """Was the verdict read exactly as the model sent it, with no repair?

    The cache stores only such replies: a payload that came out of the
    truncation repair, a code fence or a brace slice may be a fragment of what
    the model meant (a reply cut after the first signal), and a second call
    would then be served that fragment as if it were the whole ruling.
    """
    choices = result.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    calls = message.get("tool_calls") or []
    if calls:
        raw = (calls[0].get("function") or {}).get("arguments")
        if isinstance(raw, dict):
            return True
    else:
        raw = message.get("content")
        raw = raw.strip() if isinstance(raw, str) else raw
    if not isinstance(raw, str):
        return False
    try:
        return isinstance(_loads_verdict(raw), dict)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return False


def cached_reply(payload: dict[str, Any]) -> dict[str, Any]:
    """A cached verdict payload dressed as the tool-call reply it came from.

    A cache hit is replayed through the same parsing and checks as a live
    reply, so a cached row can never be trusted more than a fresh one. No
    ``usage``: a served verdict spent no tokens.
    """
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"type": "function", "function": {"arguments": json.dumps(payload)}}
                    ],
                }
            }
        ],
        "usage": {},
    }


def _verdict_is_complete(parsed: dict[str, Any], amb: list[SemanticSignal]) -> bool:
    """Is this a whole coherence ruling: every required field, a verdict per ambiguous signal?

    A reply that dropped a field, or ruled on only some of the signals it was
    asked about, is still usable once, but is not something to serve again
    for the same question.
    """
    if _coerce_verdict(parsed.get("pass")) is None:
        return False
    confidence = parsed.get("confidence")
    if (
        not isinstance(parsed.get("reason"), str)
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or parsed.get("failure_kind") not in _FAILURE_KINDS
    ):
        return False
    given = parsed.get("disambiguation_verdicts")
    ruled = {
        v.get("sig_id")
        for v in (given if isinstance(given, list) else [])
        if isinstance(v, dict) and _coerce_verdict(v.get("is_failure")) is not None
    }
    return all(s.sig_id in ruled for s in amb)


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
    temperature: float = 0.0,
    seed: int | None = None,
    cache: verdict_cache.CacheScope | None = None,
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

    ``temperature``/``seed`` are the sampling the request asks for; the verdict records them
    and whether it was ``deterministic`` (``determinism.stamp``). No seed is sent unless set.

    ``cache`` (see :mod:`argus.verdict_cache`) answers an identical request from disk: the
    key is the whole request as sent, evidence and history included, and a served verdict
    comes back ``cached=True`` with zero tokens. ``None`` (the default) never touches the
    cache. The cache is only used for a request that can claim to be reproducible
    (``determinism.gate``: temperature 0.0, a seed, and a provider that honors it); any other
    request is judged live and never stored.
    """
    t0 = time.perf_counter()

    from argus.llm_proxy import create_chat_completion, is_available, judge_route

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

    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        **determinism.sampling_params(temperature, seed),
        "response_format": {"type": "json_object"},
        "tools": [JUDGE_VERDICT_TOOL],
        "tool_choice": JUDGE_TOOL_CHOICE,
    }
    # The key is taken from the request exactly as it is sent, after the evidence and
    # history are in the prompt: a verdict ruled with a failing validator in view is never
    # served to a call that has none.
    route = judge_route(model) if cache is not None else ""
    cache = determinism.gate(cache, route, temperature, seed)
    cache_key = ""
    cached_payload = None
    if cache is not None:
        cache_key = verdict_cache.request_key(request, route=route)
        cached_payload = verdict_cache.lookup(cache, cache_key, model)
        if cached_payload is not None and not _verdict_is_complete(cached_payload, amb):
            cached_payload = None  # not a whole ruling: ask again

    try:
        result = (
            cached_reply(cached_payload)
            if cached_payload is not None
            else create_chat_completion(**request, timeout=8.0 if amb else 5.0)
        )
        elapsed = (time.perf_counter() - t0) * 1000

        if "error" in result:
            return _skip_result(f"check skipped: {result['error']}", model, elapsed), []

        choices = result.get("choices", [])
        if not choices:
            # No completion at all — the judge never ruled on anything.
            return _skip_result("check skipped: judge returned no completion", model, elapsed), []
        parsed = _parse_verdict_payload(result)
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

        stamp = determinism.stamp(temperature, seed, result, cached=cached_payload is not None)
        if (
            cache is not None
            and cached_payload is None
            and stamp["deterministic"]
            and reply_is_clean(result)
            and _verdict_is_complete(parsed, amb)
        ):
            verdict_cache.store(cache, cache_key, model, parsed)

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
            **stamp,
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
