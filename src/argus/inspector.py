from __future__ import annotations

import re
from difflib import SequenceMatcher
from statistics import median
from typing import Any

from argus.heuristic_engine import scan_execution_output as _scan_execution_output
from argus.models import FieldMismatch, InspectionResult, SemanticSignal, ToolFailure
from argus.utils.type_introspection import extract_fields, get_node_state_type

_EMPTY_VALUES = (None, "", [], {})
_PRIMITIVE_TYPES = (str, int, float, bool)


def _is_empty_element(v: Any) -> bool:
    """True if a collection element carries no content (null, blank, or empty).

    Numeric 0/False are NOT empty — they're valid data. Only None, whitespace-only
    strings, and empty collections count.
    """
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) == 0
    return False

# ── Truncation detection helpers ─────────────────────────────────────────────

_TRUNCATION_RE = re.compile(r"\w$")
_TERMINAL_PUNCT = re.compile(r'[.!?;:,)\]}"\'`]')


def _is_truncated(s: str) -> bool:
    """Return True if the string appears to be cut off mid-sentence."""
    if len(s) < 30 or " " not in s:
        return False
    # Must end with a word character (letter or digit)
    if not _TRUNCATION_RE.search(s):
        return False
    # Last 12 chars must have NO terminal punctuation
    tail = s[-12:]
    if _TERMINAL_PUNCT.search(tail):
        return False
    # Short strings (< 200 chars) are likely titles, labels, or short
    # self-contained text — they don't need sentence-ending punctuation.
    if len(s) < 200:
        return False
    # If the string ends with a complete number (year, etc.) it's likely
    # a topic/title, not truncated mid-word
    last_word = s.rsplit(None, 1)[-1] if s.rsplit(None, 1) else ""
    if re.fullmatch(r"\d{2,}", last_word):
        return False
    # Only flag as truncated if the last word looks like a fragment:
    # a single lowercase letter or very short (1-2 char) non-word.
    # Complete words like "markets" or "analysis" are not fragments.
    if len(last_word) >= 3:
        return False
    return True


# ── Confidence-mismatch helpers ───────────────────────────────────────────────

_SUCCESS_FIELD_NAMES = {"success", "ok", "succeeded", "is_valid", "is_ok", "status"}
_SUCCESS_STRING_VALUES = {"ok", "success", "retrieved successfully", "done", "completed"}
_CONFIDENCE_FIELD_NAMES = {
    "confidence",
    "score",
    "certainty",
    "probability",
    "quality_score",
    "confidence_score",
    "validation_score",
    "security_score",
}
_RETRIEVAL_CONTENT_KEYS = {"content", "text", "body", "chunk"}
_SUMMARY_FIELD_RE = re.compile(
    r"(summary|synthesis|report|analysis|conclusion|findings)",
    re.IGNORECASE,
)

# ── Input-output coherence helpers ────────────────────────────────────────────

_CONTEXT_OVERFLOW_THRESHOLD = 100_000  # chars — ~25K tokens, proxy for overflow

# Antonym pairs for lightweight contradiction detection (Rule 15)
_ANTONYM_PAIRS: list[tuple[frozenset[str], frozenset[str]]] = [
    (
        frozenset({"bullish", "buy", "long", "upside", "uptrend", "optimistic"}),
        frozenset({"bearish", "sell", "short", "downside", "downtrend", "pessimistic"}),
    ),
    (
        frozenset({"increase", "higher", "grow", "rise", "gain", "up"}),
        frozenset({"decrease", "lower", "shrink", "fall", "loss", "down"}),
    ),
    (
        frozenset({"approve", "accept", "valid", "pass", "allowed"}),
        frozenset({"reject", "deny", "invalid", "fail", "blocked"}),
    ),
    (
        frozenset({"safe", "secure", "trusted", "compliant"}),
        frozenset({"unsafe", "insecure", "untrusted", "vulnerable"}),
    ),
]

# SP-series hedging phrases (must stay in sync with signatures.json SP-009..SP-018)
_HEDGING_PHRASES = [
    "i'm not sure",
    "not certain",
    "i'm unsure",
    "i cannot be certain",
    "i am not sure",
    # also include existing SP-001..SP-008 phrases for completeness
    "as an ai",
    "i cannot",
    "i'm sorry but",
    "i don't have access",
    "i am unable to",
    "i apologize",
    "my knowledge cutoff",
    "i don't have the ability",
    "i don't know",
    "i do not know",
    "i don't have enough information",
    "i do not have enough information",
]

# ── Tool output detection patterns ───────────────────────────────────────────

_ERROR_KEYS = {"error", "error_message", "err", "errors", "exception", "error_detail"}
_STATUS_KEYS = {"status_code", "status", "http_status", "code", "response_code"}
# Boolean fields whose False/True value indicates an error condition
_SUCCESS_KEYS = {"success", "ok", "succeeded", "is_valid", "is_ok"}
_FAILURE_KEYS = {"failed", "is_error", "has_error", "errored", "is_failed"}
_RESULT_NAME_RE = re.compile(
    r"(results?|items?|documents?|records?|rows?|hits?|entries?|matches?"
    r"|findings?|output|content|data|response|answer|text|body|payload)$",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(
    r"rate.?limit|quota.?exceed|too.?many.?requests?|429",
    re.IGNORECASE,
)
_ERROR_STR_RE = re.compile(
    r"^(error|failed?|exception|timeout|unauthorized|forbidden"
    r"|not[ _]found|service[ _]unavailable)",
    re.IGNORECASE,
)

_SEVERITY_RANK = {"critical": 2, "warning": 1}

# Nested tool-failure scan (dict/list payloads)
_MAX_TOOL_SCAN_DEPTH = 5

# Empty lists on these keys are failed retrievals, not optional blanks.
_RETRIEVAL_LIST_KEYS = frozenset(
    {"documents", "docs", "results", "hits", "sources", "items"}
)

# Main LLM text fields — truncated output here is a node failure.
_MAIN_LLM_OUTPUT_KEYS = frozenset({"answer", "draft", "summary", "reply", "content"})

# Intermediate fields whose copy into a differently named output is a handoff,
# not "the model echoed the user prompt."
_HANDOFF_SOURCE_KEYS = frozenset(
    {
        "draft",
        "summary",
        "analysis",
        "report",
        "findings",
        "context",
        "ranked",
        "documents",
        "answer",
        "reply",
        "recommendation",
        "synthesis",
        "response",
    }
)

# Unknown-key ↔ schema matching: typo-sized edits only (see _is_likely_typo).
_UNKNOWN_KEY_MAX_EDIT = 2
_UNKNOWN_KEY_MAX_LEN_DIFF = 3

# ── Semantic registry failure-type mapping ────────────────────────────────────

_CATEGORY_TO_FAILURE: dict[str, str] = {
    "placeholder_outputs": "placeholder_detected",
    "null_like_semantic": "placeholder_detected",
    "suspicious_phrases": "semantic_degradation",
    "corrupted_markers": "semantic_degradation",
    "malformed_payload": "semantic_degradation",
    "repeated_filler": "semantic_degradation",
    "empty_semantic_state": "structural_anomaly",
    "semantic_refusal": "semantic_degradation",
}


def _coerce_http_status(value: Any) -> int | None:
    """Coerce int or numeric-string HTTP codes. Ignore bools and non-numeric strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _leaf_key(field_path: str) -> str:
    """Last path component, without list-index suffixes."""
    leaf = field_path.rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", leaf)


def _is_retrieval_list_key(field_path: str) -> bool:
    return _leaf_key(field_path).lower() in _RETRIEVAL_LIST_KEYS


def _empty_result_severity(field_path: str, value: Any) -> str:
    """Empty retrieval lists fail the node; other empty result-like fields warn."""
    if _is_retrieval_list_key(field_path) and isinstance(value, list):
        return "critical"
    if _is_retrieval_list_key(field_path) and value is None:
        return "critical"
    return "warning"


def _add_http_status_failure(
    add: Any,
    field_path: str,
    status: int,
    nested: bool,
) -> None:
    prefix = "nested " if nested else ""
    if status == 429:
        add(
            ToolFailure(
                failure_type="rate_limit",
                field_name=field_path,
                severity="warning",
                evidence=f"{prefix}HTTP 429 rate limit".strip(),
            )
        )
    else:
        add(
            ToolFailure(
                failure_type="error_response",
                field_name=field_path,
                severity="critical",
                evidence=f"{prefix}HTTP {status} error response".strip(),
            )
        )


def _apply_tool_shape_rules(
    key: str,
    value: Any,
    field_path: str,
    depth: int,
    add: Any,
) -> None:
    """Apply error / HTTP / empty-result / partial-failure rules at one field."""
    nested = depth > 0
    key_l = key.lower()

    # Rule 1 — error key with truthy value
    if key in _ERROR_KEYS:
        if value:
            as_str = str(value)
            if _RATE_LIMIT_RE.search(as_str):
                add(
                    ToolFailure(
                        failure_type="rate_limit",
                        field_name=field_path,
                        severity="warning",
                        evidence=(
                            f"{'nested ' if nested else ''}"
                            f"rate limit detected: {as_str[:120]!r}"
                        ),
                    )
                )
            else:
                add(
                    ToolFailure(
                        failure_type="error_response",
                        field_name=field_path,
                        severity="critical",
                        evidence=(
                            f"{'nested error field' if nested else 'error field set'}: "
                            f"{as_str[:120]!r}"
                        ),
                    )
                )
        return

    # Rule 2 — HTTP status (int or numeric string)
    if key in _STATUS_KEYS:
        status = _coerce_http_status(value)
        if status is not None and 400 <= status <= 599:
            _add_http_status_failure(add, field_path, status, nested)
            return

    # Rule 2b — boolean success field set to False
    if key_l in _SUCCESS_KEYS and isinstance(value, bool) and not value:
        add(
            ToolFailure(
                failure_type="error_response",
                field_name=field_path,
                severity="critical",
                evidence=(
                    f"{'nested ' if nested else ''}success indicator '{key}' is False"
                ),
            )
        )
        return

    # Rule 2c — boolean failure field set to True
    if key_l in _FAILURE_KEYS and isinstance(value, bool) and value:
        add(
            ToolFailure(
                failure_type="error_response",
                field_name=field_path,
                severity="critical",
                evidence=(
                    f"{'nested ' if nested else ''}failure indicator '{key}' is True"
                ),
            )
        )
        return

    # Rule 3 — empty result field with results-like name
    if _RESULT_NAME_RE.search(key):
        if value is None or value == [] or value == {} or value == "":
            add(
                ToolFailure(
                    failure_type="empty_result",
                    field_name=field_path,
                    severity=_empty_result_severity(field_path, value),
                    evidence="tool returned no results",
                )
            )
            return
        if isinstance(value, (list, dict)):
            items = value if isinstance(value, list) else list(value.values())
            if items and all(_is_empty_element(v) for v in items):
                add(
                    ToolFailure(
                        failure_type="empty_result",
                        field_name=field_path,
                        severity=_empty_result_severity(
                            field_path, [] if isinstance(value, list) else value
                        ),
                        evidence=f"tool returned {len(items)} items but all are empty/null",
                    )
                )
                return

    # Rule 4 — error string in a non-error field
    if isinstance(value, str) and _ERROR_STR_RE.match(value):
        add(
            ToolFailure(
                failure_type="error_in_data",
                field_name=field_path,
                severity="warning",
                evidence=f"field contains error-like string: {value[:80]!r}",
            )
        )
        return

    # Rule 5 — partial failure inside a list
    if isinstance(value, list) and value:
        error_count = sum(1 for item in value if isinstance(item, dict) and item.get("error"))
        if error_count > 0:
            add(
                ToolFailure(
                    failure_type="partial_failure",
                    field_name=field_path,
                    severity="warning",
                    evidence=f"{error_count} of {len(value)} items contain errors",
                )
            )


def _scan_payload_for_tool_failures(
    obj: Any,
    prefix: str,
    depth: int,
    add: Any,
) -> None:
    """Recursively scan dict/list payloads for tool-failure shapes (max depth 5)."""
    if depth > _MAX_TOOL_SCAN_DEPTH:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            field_path = f"{prefix}.{key}" if prefix else str(key)
            _apply_tool_shape_rules(key, value, field_path, depth, add)
            if isinstance(value, dict):
                _scan_payload_for_tool_failures(value, field_path, depth + 1, add)
            elif isinstance(value, list):
                _scan_payload_for_tool_failures(value, field_path, depth + 1, add)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                item_path = f"{prefix}[{i}]"
                _scan_payload_for_tool_failures(item, item_path, depth + 1, add)


def is_legitimate_field_handoff(
    input_state: dict[str, Any] | None,
    output_dict: dict[str, Any] | None,
) -> bool:
    """True when output copies an intermediate field under a new name.

    Used to keep passthrough closers (draft→reply) from being judged as
    the model echoing the user prompt.
    """
    if not input_state or not output_dict:
        return False
    for out_key, out_val in output_dict.items():
        if not isinstance(out_val, str) or len(out_val) < 80:
            continue
        for in_key, in_val in input_state.items():
            if not isinstance(in_val, str) or len(in_val) < 80:
                continue
            if in_key.lower() not in _HANDOFF_SOURCE_KEYS:
                continue
            if out_key.lower() == in_key.lower():
                continue
            if out_val == in_val or (
                len(out_val) > 40
                and len(in_val) > 40
                and (out_val[:80] == in_val[:80] or out_val[-80:] == in_val[-80:])
            ):
                return True
    return False


def inspect_tool_outputs(
    output_dict: dict[str, Any],
    strict: bool = False,
    _precomputed_signals: list[SemanticSignal] | None = None,
    input_state: dict[str, Any] | None = None,
    reducer_fields: dict[str, Any] | None = None,
) -> InspectionResult:
    """Scan a node's output dict for tool call failure patterns.

    Detects: error keys, HTTP error codes (int or numeric string), empty
    retrieval/result fields, error strings in data fields, and partial
    failures inside lists. Recursively scans dict/list payloads up to
    depth 5 so nested ``success: false`` / HTTP 4xx–5xx can fail the node.
    Returns an InspectionResult with tool_failures and semantic_signals populated.

    strict: if True, all warning-severity failures are promoted to critical.
    _precomputed_signals: if provided, reuse these SemanticSignals for Rule 7
        instead of re-scanning. Avoids double-scan when called from
        inspect_transition which already ran the heuristic scan.
    """
    # field_name → best ToolFailure so far (highest severity)
    by_field: dict[str, ToolFailure] = {}

    def _add(tf: ToolFailure) -> None:
        existing = by_field.get(tf.field_name)
        if existing is None or _SEVERITY_RANK[tf.severity] > _SEVERITY_RANK[existing.severity]:
            by_field[tf.field_name] = tf

    # Rules 1–6 — recursive tool-failure shapes (error keys, HTTP status,
    # success/failure booleans, empty retrieval, nested dict/list payloads)
    _scan_payload_for_tool_failures(output_dict, "", 0, _add)

    # Rule 7 — deep recursive semantic heuristic scan
    # Use pre-computed signals if available (avoids double-scan)
    signals = (
        _precomputed_signals
        if _precomputed_signals is not None
        else _scan_execution_output(output_dict)
    )
    for signal in signals:
        # Confidence gating: skip low-confidence matches, downgrade ambiguous ones
        if signal.confidence < 0.3:
            continue  # likely false positive — skip ToolFailure creation
        severity = signal.severity
        if signal.confidence < 0.7:
            severity = "warning"  # ambiguous — force warning regardless of sig severity
        _add(
            ToolFailure(
                failure_type=_CATEGORY_TO_FAILURE.get(signal.category, "semantic_degradation"),
                field_name=signal.dotted_path,
                severity=severity,
                evidence=f"[{signal.sig_id}] {signal.description}: {signal.evidence}",
            )
        )

    # Rule 8 — truncated output detection
    def _flag_truncated(field_path: str, text: str) -> None:
        if not _is_truncated(text):
            return
        leaf = _leaf_key(field_path).lower()
        severity = "critical" if leaf in _MAIN_LLM_OUTPUT_KEYS else "warning"
        _add(
            ToolFailure(
                failure_type="truncated_output",
                field_name=field_path,
                severity=severity,
                evidence=f"string appears truncated mid-word: {text[-30:]!r}",
            )
        )

    for key, value in output_dict.items():
        if isinstance(value, str):
            _flag_truncated(key, value)
        elif isinstance(value, dict):
            for inner_key, inner_val in value.items():
                if isinstance(inner_val, str):
                    _flag_truncated(f"{key}.{inner_key}", inner_val)

    # Rule 9 — hallucinated success contradiction
    # Detect: success/status field is truthy AND result field is empty
    success_fields: dict[str, Any] = {}
    result_fields: dict[str, Any] = {}
    for key, value in output_dict.items():
        if key.lower() in _SUCCESS_FIELD_NAMES:
            success_fields[key] = value
        if _RESULT_NAME_RE.search(key):
            result_fields[key] = value

    for s_key, s_val in success_fields.items():
        # Determine if success field is truthy
        is_success = False
        if isinstance(s_val, bool) and s_val:
            is_success = True
        elif isinstance(s_val, str):
            if s_val.lower() in _SUCCESS_STRING_VALUES:
                is_success = True
        if not is_success:
            continue
        # Check if any result field is empty
        for r_key, r_val in result_fields.items():
            if r_val is None or r_val == [] or r_val == {} or r_val == "":
                # Use the success field name as the field_name so this doesn't
                # collide with the empty_result failure already recorded for r_key
                _add(
                    ToolFailure(
                        failure_type="confidence_mismatch",
                        field_name=f"{s_key}:{r_key}",
                        severity="warning",
                        evidence=(
                            f"claimed success ('{s_key}'={s_val!r}) but empty results in '{r_key}'"
                        ),
                    )
                )

    # Rule 10 — confidence-behavior mismatch
    # Detect: high confidence score but hedging language in the same output
    for key, value in output_dict.items():
        if key.lower() not in _CONFIDENCE_FIELD_NAMES:
            continue
        if not isinstance(value, (int, float)):
            continue
        if float(value) <= 0.80:
            continue
        # Found high-confidence field — look for hedging phrases in other string fields
        for other_key, other_val in output_dict.items():
            if other_key == key:
                continue
            if not isinstance(other_val, str):
                continue
            lower_val = other_val.lower()
            for phrase in _HEDGING_PHRASES:
                if phrase in lower_val:
                    _add(
                        ToolFailure(
                            failure_type="confidence_mismatch",
                            field_name=key,
                            severity="warning",
                            evidence=(
                                f"high confidence ({key}={value}) but hedging phrase "
                                f"'{phrase}' found in '{other_key}'"
                            ),
                        )
                    )
                    break

    # Rule 11 — retrieval quality scoring
    for key, value in output_dict.items():
        if not isinstance(value, list) or not value:
            continue
        # Check if items are dicts with a "score" key
        score_items = [item for item in value if isinstance(item, dict) and "score" in item]
        if not score_items:
            continue
        scores = []
        for item in score_items:
            try:
                scores.append(float(item["score"]))
            except (TypeError, ValueError):
                pass
        if scores:
            med = median(scores)
            if med < 0.45:
                _add(
                    ToolFailure(
                        failure_type="retrieval_quality_low",
                        field_name=key,
                        severity="warning",
                        evidence=f"median retrieval score {med:.2f}",
                    )
                )
        # Check for shallow content
        content_strings = []
        for item in score_items:
            for ck in _RETRIEVAL_CONTENT_KEYS:
                if ck in item and isinstance(item[ck], str):
                    content_strings.append(item[ck])
                    break
        if content_strings and all(len(s) < 60 for s in content_strings):
            # Use a distinct field_name suffix so this doesn't collide with
            # the retrieval_quality_low entry already stored under `key`
            _add(
                ToolFailure(
                    failure_type="shallow_context",
                    field_name=f"{key}:content_depth",
                    severity="warning",
                    evidence=f"all {len(content_strings)} content strings are < 60 chars",
                )
            )

    # Rule 12 — information density / shallow summary
    # Detect output fields whose name suggests a summary/analysis that are suspiciously short
    for key, value in output_dict.items():
        if not isinstance(value, str):
            continue
        if not _SUMMARY_FIELD_RE.search(key):
            continue
        v_len = len(value)
        if v_len < 40:
            _add(
                ToolFailure(
                    failure_type="shallow_output",
                    field_name=key,
                    severity="warning",
                    evidence=f"'{key}' is only {v_len} chars",
                )
            )
        elif input_state is not None and v_len < 100:
            # Check if input had substantial content that this summary doesn't reflect
            input_total = sum(len(v) for v in input_state.values() if isinstance(v, str))
            if input_total > 800:
                _add(
                    ToolFailure(
                        failure_type="information_compression_anomaly",
                        field_name=key,
                        severity="warning",
                        evidence=(
                            f"'{key}' is {v_len} chars but input had {input_total} chars of text"
                        ),
                    )
                )

    # Rules 13–16 — input-output coherence (all no-ops when input_state is None)
    if input_state is not None:
        # Rule 13 — selective attention reduction
        # Input has N list items, output has the same-named field with < 50% of them.
        # Skip fields that have a LangGraph reducer (e.g. operator.add) — the node
        # returns only new items; the framework merges them.
        for key, in_val in input_state.items():
            if not isinstance(in_val, list) or len(in_val) < 4:
                continue
            if reducer_fields and key in reducer_fields:
                continue  # ponytail: reducer handles merge, raw comparison invalid
            out_val = output_dict.get(key)
            if isinstance(out_val, list) and len(out_val) < len(in_val) * 0.5:
                _add(
                    ToolFailure(
                        failure_type="selective_attention_reduction",
                        field_name=key,
                        severity="warning",
                        evidence=(f"input had {len(in_val)} items, output has {len(out_val)}"),
                    )
                )

        # Rule 14 — input echo detection
        # Output string is ≥ 90% similar to any individual input string field.
        # Compare field-to-field (not output vs concatenated blob) so the ratio
        # isn't diluted when input_state has multiple fields.
        input_str_fields = [
            (k, v) for k, v in input_state.items() if isinstance(v, str) and len(v) >= 80
        ]
        if input_str_fields:
            for key, value in output_dict.items():
                if not isinstance(value, str) or len(value) < 80:
                    continue
                for in_key, in_val in input_str_fields:
                    # Legitimate pipeline handoff: closer copies draft→reply
                    # (or similar) rather than repeating the user prompt.
                    if (
                        in_key.lower() in _HANDOFF_SOURCE_KEYS
                        and key.lower() != in_key.lower()
                    ):
                        continue
                    if max(len(value), len(in_val)) > 5000:
                        ratio = 1.0 if value == in_val else (
                            1.0 - abs(len(value) - len(in_val)) / max(len(value), len(in_val))
                            if value[:500] == in_val[:500] else 0.0
                        )
                    else:
                        ratio = SequenceMatcher(None, value, in_val, autojunk=False).ratio()
                    if ratio >= 0.90:
                        _add(
                            ToolFailure(
                                failure_type="input_echo",
                                field_name=key,
                                severity="warning",
                                evidence=(f"output is {ratio:.0%} similar to input '{in_key}'"),
                            )
                        )
                        break  # one flag per output field is enough

        # Rule 15 — contradictory transformation (lightweight keyword NLI)
        input_text = " ".join(v for v in input_state.values() if isinstance(v, str)).lower()
        output_text = " ".join(v for v in output_dict.values() if isinstance(v, str)).lower()
        if input_text and output_text:
            input_words = set(re.findall(r"\w+", input_text))
            output_words = set(re.findall(r"\w+", output_text))
            for pos_set, neg_set in _ANTONYM_PAIRS:
                in_pos = input_words & pos_set
                in_neg = input_words & neg_set
                out_pos = output_words & pos_set
                out_neg = output_words & neg_set
                matched_in = matched_out = None
                if in_pos and out_neg:
                    matched_in, matched_out = next(iter(in_pos)), next(iter(out_neg))
                elif in_neg and out_pos:
                    matched_in, matched_out = next(iter(in_neg)), next(iter(out_pos))
                if matched_in and matched_out:
                    _add(
                        ToolFailure(
                            failure_type="semantic_contradiction",
                            field_name="_coherence",
                            severity="warning",
                            evidence=(f"input has '{matched_in}', output has '{matched_out}'"),
                        )
                    )
                    break  # one contradiction flag per run is enough

        # Rule 16 — context window overflow proxy
        # Strings truncated by ARGUS's capture_state appear as sentinel dicts:
        # {"__argus_truncated__": True, "type": "str", "preview": "..."}.
        # Each such field was at least 50K chars — count it as such.
        input_char_total = 0
        for v in input_state.values():
            if isinstance(v, str):
                input_char_total += len(v)
            elif isinstance(v, dict) and v.get("__argus_truncated__") and v.get("type") == "str":
                # Field was truncated by ARGUS capture — actual string was ≥50K chars.
                # Count it as exceeding the threshold on its own so one large field flags.
                input_char_total += _CONTEXT_OVERFLOW_THRESHOLD + 1
        if input_char_total > _CONTEXT_OVERFLOW_THRESHOLD:
            _add(
                ToolFailure(
                    failure_type="context_size_anomaly",
                    field_name="_input_context_size",
                    severity="warning",
                    evidence=(
                        f"input state contains ≥{input_char_total:,} chars"
                        f" (~{input_char_total // 4:,} tokens)"
                    ),
                )
            )

    # Strict mode: promote all warnings to critical
    if strict:
        for field_name, tf in list(by_field.items()):
            if tf.severity == "warning":
                by_field[field_name] = ToolFailure(
                    failure_type=tf.failure_type,
                    field_name=tf.field_name,
                    severity="critical",
                    evidence=tf.evidence,
                )

    tool_failures = list(by_field.values())
    has_tool_failure = any(tf.severity == "critical" for tf in tool_failures)
    semantic_signals_list: list[SemanticSignal] = list(signals)
    return InspectionResult(
        is_silent_failure=False,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="critical" if has_tool_failure else ("warning" if tool_failures else "ok"),
        message=_build_tool_failure_message(tool_failures) or "No tool failures detected",
        tool_failures=tool_failures,
        has_tool_failure=has_tool_failure,
        semantic_signals=semantic_signals_list,
    )


def inspect_transition(
    current_node: str,
    output_dict: dict[str, Any] | None,
    merged_state: dict[str, Any],
    successor_fns: list[Any],
    strict: bool = False,
    input_state: dict[str, Any] | None = None,
    current_node_fn: Any = None,
    reducer_fields: dict[str, Any] | None = None,
) -> InspectionResult:
    """Check if the output of current_node will cause a silent failure in any successor.

    Also scans the output dict for tool call failure patterns (empty results,
    error responses, rate limits, etc.) independent of successor analysis.

    Args:
        current_node: name of the node that just ran
        output_dict: the dict returned by the node (may be None on crash)
        merged_state: the full state after merging the output (what successor sees)
        successor_fns: list of callable node functions that may run next
    """
    # Scan heuristic signals ONCE, then pass to both tool failure conversion
    # and semantic_signals storage. Previously _scan_execution_output was
    # called twice (once inside inspect_tool_outputs Rule 7, once here),
    # causing double-counting in correlator weight calculations.
    semantic_signals: list[SemanticSignal] = (
        _scan_execution_output(output_dict) if output_dict else []
    )
    _tool_result = (
        inspect_tool_outputs(
            output_dict,
            strict=strict,
            _precomputed_signals=semantic_signals,
            input_state=input_state,
            reducer_fields=reducer_fields,
        )
        if output_dict
        else None
    )
    tool_failures = _tool_result.tool_failures if _tool_result is not None else []

    # Silent no-op: the node ran but returned an empty state update ({}) — no
    # keys at all. With successors waiting downstream it has produced nothing
    # for them to consume — the canonical silent failure the ORIGIN node commits
    # (README: "node returns {} or drops a required field"). Without this the
    # drop is invisible and blame falls on the downstream crash site instead.
    # Only literal {} is flagged here: a dict WITH keys (even empty-valued ones,
    # e.g. {"vulnerabilities": []}) is a real state contribution and is judged
    # by the per-field rules above. Conditional/router nodes reach here with
    # successor_fns=[] (see ArgusSession._get_successor_fns) and are exempt.
    if successor_fns and output_dict is not None and not output_dict:
        tool_failures = [
            *tool_failures,
            ToolFailure(
                failure_type="empty_output",
                field_name="_output",
                severity="critical",
                evidence="node returned an empty state update — no fields produced",
            ),
        ]

    has_tool_failure = any(tf.severity == "critical" for tf in tool_failures)

    if output_dict is None:
        return InspectionResult(
            is_silent_failure=False,
            missing_fields=[],
            empty_fields=[],
            type_mismatches=[],
            severity="ok",
            message="Node crashed — no output to inspect",
            tool_failures=[],
            has_tool_failure=False,
        )

    if not successor_fns:
        severity = "critical" if has_tool_failure else ("warning" if tool_failures else "ok")
        message = (
            _build_tool_failure_message(tool_failures) or "No successor nodes to validate against"
        )
        return InspectionResult(
            is_silent_failure=False,
            missing_fields=[],
            empty_fields=[],
            type_mismatches=[],
            severity=severity,
            message=message,
            tool_failures=tool_failures,
            has_tool_failure=has_tool_failure,
            semantic_signals=semantic_signals,
        )

    all_missing: list[str] = []
    all_empty: list[str] = []
    all_mismatches: list[FieldMismatch] = []
    unannotated: list[str] = []
    suspicious_empty: list[str] = []
    worst_severity = "ok"
    annotated_count = 0

    # Fields this node actually wrote — used to skip checks for fields that
    # are a parallel sibling's responsibility (fan-out/fan-in pattern).
    node_provided_keys: set[str] = set(output_dict.keys()) if output_dict else set()

    for fn in successor_fns:
        fn_name = _get_fn_name(fn)
        state_type = get_node_state_type(fn)
        if state_type is None:
            unannotated.append(fn_name)
            continue
        fields = extract_fields(state_type)
        if not fields:
            unannotated.append(fn_name)
            continue

        annotated_count += 1
        missing, empty, mismatches = _check_fields(
            fields,
            merged_state,
            node_provided_keys,
            strict=strict,
            input_state=input_state,
        )

        for f in missing:
            if f not in all_missing:
                all_missing.append(f)
        for f in empty:
            if f not in all_empty:
                all_empty.append(f)
        for m in mismatches:
            if m not in all_mismatches:
                all_mismatches.append(m)

    # Unknown key detection: if a node writes keys that do NOT exist in any
    # successor's schema, no downstream node will ever read them.  This is
    # always a bug — typo, wrong prefix, completely wrong name, etc.
    #
    # When unknown keys are found, check which schema fields are NEWLY
    # missing — i.e. not in merged state AND not in the node's input either
    # (meaning THIS node was supposed to produce them but wrote the wrong key).
    if annotated_count > 0 and output_dict:
        all_schema_fields: set[str] = set()
        for fn in successor_fns:
            st = get_node_state_type(fn)
            if st is not None:
                all_schema_fields.update(extract_fields(st).keys())
        # Find keys the node NEWLY added (not inherited from input).
        # LangGraph nodes return {**state, "new_key": val}, so output
        # contains input keys plus new ones.  Compare against input_state
        # to find what this node actually contributed.
        _input_keys = set(input_state.keys()) if input_state else set()
        novel_keys = {k for k in output_dict if k not in _input_keys}

        # Unknown novel keys: new keys that aren't in any successor schema
        unknown_novel = [k for k in novel_keys if k not in all_schema_fields]

        if unknown_novel:
            # The node produced N unknown keys — it was probably supposed
            # to write N schema keys instead.  Find the best-matching
            # missing schema fields (at most one per unknown key).
            candidates: list[str] = []
            for field_name in all_schema_fields:
                if field_name in _input_keys:
                    continue  # existed before this node — not our job
                if field_name in merged_state and not _is_empty(merged_state.get(field_name)):
                    continue  # present and non-empty — fine
                candidates.append(field_name)

            # Match unknown keys to candidates by edit distance only when the
            # names look like typos (edit distance ≤ 2, length diff ≤ 3).
            # Unrelated extras (status_code vs attempts) must not invent
            # contract violations.
            matched: list[str] = []
            for uk in unknown_novel:
                best_field = None
                best_dist = 999
                for c in candidates:
                    if c in matched:
                        continue
                    d = _edit_distance(uk, c)
                    if d < best_dist:
                        best_dist = d
                        best_field = c
                if best_field is not None and _is_likely_typo([uk], best_field):
                    matched.append(best_field)

            for field_name in matched:
                if field_name not in all_missing:
                    all_missing.append(field_name)

    # Upstream propagation: if this node's own schema expects a field that
    # is missing from its input, an upstream node failed to produce it.
    # We detect this by checking: the node's output contains a field with
    # a fallback/empty value that matches a schema field the node tried to
    # read from state (e.g. state.get("draft") returned "" because draft
    # was never set).  The output is technically valid but degraded.
    #
    # Simple reliable heuristic: if a schema field was NOT in the input
    # AND this node wrote an empty/fallback value for a DIFFERENT field
    # that depends on it, the output is degraded.  But we can't know
    # inter-field dependencies statically.
    #
    # Instead, delegate to build_root_cause_chain() which walks the event
    # history and has full context.  The per-node inspector focuses on
    # what THIS node did wrong, not what upstream broke.

    # Fallback heuristic: when ALL successors lack annotations, check if the
    # current node's output contains None/empty values — these are suspicious
    # because a downstream node may silently receive degraded state.
    if unannotated and annotated_count == 0 and output_dict:
        for key, value in output_dict.items():
            if _is_empty(value) and key not in suspicious_empty:
                suspicious_empty.append(key)

    is_silent_failure = bool(all_missing) or (strict and bool(all_mismatches))

    if all_missing or has_tool_failure or (strict and all_mismatches):
        worst_severity = "critical"
    elif all_empty or all_mismatches or tool_failures:
        worst_severity = "warning"
    elif unannotated and suspicious_empty:
        worst_severity = "warning"
    elif unannotated:
        worst_severity = "info"

    message = _build_message(
        current_node,
        all_missing,
        all_empty,
        all_mismatches,
        unannotated,
        suspicious_empty,
        tool_failures,
    )

    return InspectionResult(
        is_silent_failure=is_silent_failure,
        missing_fields=all_missing,
        empty_fields=all_empty,
        type_mismatches=all_mismatches,
        severity=worst_severity,
        message=message,
        unannotated_successors=unannotated,
        suspicious_empty_keys=suspicious_empty,
        tool_failures=tool_failures,
        has_tool_failure=has_tool_failure,
        semantic_signals=semantic_signals,
    )


def _check_fields(
    expected_fields: dict[str, dict],
    actual_state: dict[str, Any],
    node_provided_keys: set[str] | None = None,
    strict: bool = False,
    input_state: dict[str, Any] | None = None,
) -> tuple[list[str], list[str], list[FieldMismatch]]:
    """Check successor field requirements against actual state.

    node_provided_keys: keys the current node actually wrote to output_dict.
    When supplied, fields absent from this set are treated as "another node's
    responsibility" (parallel sibling pattern) and are not flagged as missing
    UNLESS the field was also absent from input_state (upstream propagation).

    input_state: the state BEFORE this node ran.  Used to detect upstream
    propagation — if a field is missing from both input and merged state,
    an upstream node failed to produce it and this node is operating on
    degraded input.

    strict: when True, fields absent from node_provided_keys are still checked
    if they are also absent from actual_state (BS-01: silent field drop detection).
    """
    missing = []
    empty = []
    mismatches = []

    _input = input_state or {}

    for field_name, meta in expected_fields.items():
        required = meta.get("required", True)
        expected_type = meta.get("type")

        if field_name not in actual_state or _is_empty(actual_state.get(field_name)):
            if node_provided_keys is not None and field_name not in node_provided_keys:
                # This node didn't write this field.
                if field_name in _input and not _is_empty(_input.get(field_name)):
                    # Field WAS in input with a value but is now gone/empty
                    # in merged state → this node dropped it
                    missing.append(field_name)
                elif field_name in actual_state and _is_empty(actual_state.get(field_name)):
                    empty.append(field_name)
                # else: field not in input, not in output → downstream
                # will produce it.  Skip.
                continue
            if field_name not in actual_state:
                if required:
                    missing.append(field_name)
            else:
                # present but empty
                if required:
                    missing.append(field_name)
                else:
                    empty.append(field_name)
            continue

        value = actual_state[field_name]

        # lightweight type coherence check (primitives only)
        if expected_type is not None and isinstance(expected_type, type):
            if issubclass(expected_type, _PRIMITIVE_TYPES):
                if not isinstance(value, expected_type):
                    mismatches.append(
                        FieldMismatch(
                            field_name=field_name,
                            expected_type=expected_type.__name__,
                            actual_type=type(value).__name__,
                            actual_value_repr=repr(value)[:100],
                        )
                    )

        # BS-05: generic list type checking (list[str], list[int], etc.)
        origin = getattr(expected_type, "__origin__", None)
        if origin is list:
            args = getattr(expected_type, "__args__", None)
            if args and isinstance(value, list) and value:
                elem_type = args[0]
                if isinstance(elem_type, type) and issubclass(elem_type, _PRIMITIVE_TYPES):
                    bad = [i for i in value[:5] if not isinstance(i, elem_type)]
                    if bad:
                        mismatches.append(
                            FieldMismatch(
                                field_name=field_name,
                                expected_type=f"list[{elem_type.__name__}]",
                                actual_type=f"list[{type(bad[0]).__name__}]",
                                actual_value_repr=repr(bad[0])[:100],
                            )
                        )

    return missing, empty, mismatches


def _is_likely_typo(unknown_keys: list[str], schema_field: str) -> bool:
    """Check if any unknown key looks like a typo of schema_field.

    Uses Levenshtein edit distance: if an unknown key is within 2 edits
    of the schema field, it's likely a typo (e.g. "sumary" vs "summary",
    "compresd" vs "compressed").
    """
    for key in unknown_keys:
        if abs(len(key) - len(schema_field)) > _UNKNOWN_KEY_MAX_LEN_DIFF:
            continue
        if _edit_distance(key, schema_field) <= _UNKNOWN_KEY_MAX_EDIT:
            return True
    return False


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein edit distance between two strings."""
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j in range(1, len(b) + 1):
        curr = [j] + [0] * len(a)
        for i in range(1, len(a) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[i] = min(curr[i - 1] + 1, prev[i] + 1, prev[i - 1] + cost)
        prev = curr
    return prev[len(a)]


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, tuple, dict, set)) and len(value) == 0:
        return True
    return False


def _get_fn_name(fn: Any) -> str:
    """Best-effort human-readable name for a function."""
    import functools

    # Unwrap functools.partial to get the underlying function's name
    unwrapped = fn
    while isinstance(unwrapped, functools.partial):
        unwrapped = unwrapped.func

    name = getattr(unwrapped, "__name__", None)
    if name:
        return name
    name = getattr(unwrapped, "__qualname__", None)
    if name:
        return name
    return repr(fn)


def _build_tool_failure_message(tool_failures: list[ToolFailure]) -> str:
    if not tool_failures:
        return ""
    parts = [f'{tf.failure_type} on "{tf.field_name}": {tf.evidence}' for tf in tool_failures]
    return "Tool failures: " + "; ".join(parts)


def _build_message(
    node: str,
    missing: list[str],
    empty: list[str],
    mismatches: list[FieldMismatch],
    unannotated: list[str] | None = None,
    suspicious_empty: list[str] | None = None,
    tool_failures: list[ToolFailure] | None = None,
) -> str:
    parts = []
    if missing:
        parts.append(f"Missing required fields: {', '.join(missing)}")
    if empty:
        parts.append(f"Empty fields: {', '.join(empty)}")
    if mismatches:
        mismatch_strs = [
            f"'{m.field_name}' (expected {m.expected_type}, got {m.actual_type})"
            for m in mismatches
        ]
        parts.append(f"Type mismatches: {', '.join(mismatch_strs)}")
    if unannotated:
        names = ", ".join(unannotated)
        parts.append(f"Unannotated successors (silent-failure detection skipped): {names}")
    if suspicious_empty:
        parts.append(
            f"Suspicious empty output keys (may degrade downstream): {', '.join(suspicious_empty)}"
        )
    if tool_failures:
        tf_parts = [f'{tf.failure_type} on "{tf.field_name}"' for tf in tool_failures]
        parts.append(f"Tool failures: {', '.join(tf_parts)}")
    if not parts:
        return "All checks passed"
    return "; ".join(parts)


def _extract_missing_key_from_exception(exc_str: str) -> str | None:
    """Extract the missing key name from a KeyError traceback.

    Handles quoted, unquoted, and ``KeyError('name')`` forms used by CPython
    and LangGraph wrappers.
    """
    if not exc_str:
        return None
    patterns = (
        r"KeyError:\s*'([^']+)'",
        r'KeyError:\s*"([^"]+)"',
        r"KeyError\(\s*'([^']+)'\s*\)",
        r'KeyError\(\s*"([^"]+)"\s*\)',
        r"KeyError:\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    )
    for pat in patterns:
        m = re.search(pat, exc_str)
        if m:
            return m.group(1)
    return None


def _build_predecessor_map(
    edge_map: dict[str, list[str]],
) -> dict[str, set[str]]:
    """Build transitive predecessor sets: {node: set of all upstream nodes}."""
    # Invert edges: child → set of parents
    parents: dict[str, set[str]] = {}
    for src, dests in edge_map.items():
        for dst in dests:
            parents.setdefault(dst, set()).add(src)

    # BFS transitive closure
    result: dict[str, set[str]] = {}
    for node in set(edge_map.keys()) | {d for ds in edge_map.values() for d in ds}:
        visited: set[str] = set()
        queue = list(parents.get(node, set()))
        while queue:
            nxt = queue.pop()
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.extend(parents.get(nxt, set()))
        result[node] = visited
    return result


def build_root_cause_chain(
    steps_so_far: list[Any],
    edge_map: dict[str, list[str]] | None = None,
) -> list[str]:
    """Walk backward through NodeEvents to find where a failure first originated.

    Deduplication is keyed by (node_name, attempt_index), preserving chronological
    order of first occurrence. In an acyclic run every node has attempt_index 0, so
    each node still appears at most once. In a cyclic graph a node that fails on
    more than one iteration is cited once per failing attempt — losing that
    distinction hid which iteration actually broke.

    Parallel fan-out guard: fields provided by any node in the run are excluded
    from "missing field" blame. A field flagged missing on analyst_a is not a
    root cause if analyst_b actually provided it — they ran simultaneously.

    Crash-trace: when a node crashes with a KeyError/AttributeError, the chain
    traces back to the nearest *graph predecessor* that should have produced the
    missing field but didn't. Requires edge_map to identify actual predecessors;
    without it, falls back to step-order heuristic (legacy behavior).

    Args:
        steps_so_far: list of NodeEvent objects from the run.
        edge_map: graph topology {node: [successor_nodes]}.
    """
    # Fields actually produced by any node across the entire run
    all_provided: set[str] = set()
    for event in steps_so_far:
        if event.output_dict:
            all_provided.update(event.output_dict.keys())

    # Build predecessor map for topology-aware crash tracing
    predecessor_map = _build_predecessor_map(edge_map) if edge_map else {}

    # Index: which fields each node produced (for crash-trace)
    fields_by_node: dict[str, set[str]] = {}
    for event in steps_so_far:
        if event.output_dict and event.status not in ("crashed", "skipped"):
            existing = fields_by_node.get(event.node_name, set())
            existing.update(event.output_dict.keys())
            fields_by_node[event.node_name] = existing

    chain: list[str] = []
    # Keyed by (node_name, attempt_index) so a node that fails on a later
    # retry in a cyclic graph isn't silently deduped away by its earlier,
    # successful attempt.
    seen_nodes: set[tuple[str, int]] = set()
    seen_bad_fields: set[str] = set()

    # Phase 1: trace crash exceptions back to the upstream node that omitted
    # the required field.
    for event in reversed(steps_so_far):
        if event.status != "crashed" or not event.exception:
            continue
        missing_key = _extract_missing_key_from_exception(event.exception)
        if not missing_key:
            continue

        # If a non-empty value was already provided by ANY node that ran
        # before the crash, the crashed node had it available — this is a
        # self-contained crash, not an upstream omission.
        key_was_available = any(
            prev.output_dict is not None
            and missing_key in prev.output_dict
            and not _is_empty(prev.output_dict.get(missing_key))
            and prev.step_index < event.step_index
            and prev.status != "crashed"
            for prev in steps_so_far
        )
        if key_was_available:
            continue

        # Determine actual graph predecessors of the crashed node
        upstream = predecessor_map.get(event.node_name, set())

        # Walk backward. Skip pure passthrough nodes that never had the key
        # so a retrieve→rerank→synthesize drop blames retrieve, not rerank.
        origin = None
        for prev in reversed(steps_so_far):
            if prev.step_index >= event.step_index:
                continue
            if prev.status == "crashed":
                continue
            if upstream and prev.node_name not in upstream:
                continue
            if prev.output_dict is None:
                continue
            out = prev.output_dict
            inp = prev.input_state or {}
            if missing_key in out and not _is_empty(out.get(missing_key)):
                break
            dropped = (
                missing_key in inp
                and not _is_empty(inp.get(missing_key))
                and (missing_key not in out or _is_empty(out.get(missing_key)))
            )
            novel_keys = set(out.keys()) - set(inp.keys())
            is_passthrough = bool(inp) and set(out.keys()) <= set(inp.keys())
            if dropped or novel_keys or not is_passthrough:
                origin = prev
                break
            if origin is None:
                origin = prev

        if origin is not None:
            prev_key = (origin.node_name, origin.attempt_index)
            if prev_key not in seen_nodes:
                chain.append(origin.node_name)
                seen_nodes.add(prev_key)

    # Phase 2: inspection-based chain (silent failures, missing fields,
    # semantic degradation, tool failures, etc.)
    # Only tag nodes that ORIGINATE failures, not those that merely propagate
    # a bad value unchanged from their input.
    for event in reversed(steps_so_far):
        # Skip nodes operating on degraded input — they are victims, not causes
        if event.status in ("degraded_input", "retried", "skipped"):
            continue

        insp = event.inspection

        # Check for LLM semantic checker failure (semantic_check.passed == False)
        has_semantic_check_failure = (
            getattr(event, "semantic_check", None) is not None and not event.semantic_check.passed
        )

        # Also treat status == "semantic_fail" as a failure signal even if
        # inspection doesn't have semantic_signals (the LLM judge sets status
        # directly without populating inspection.semantic_signals).
        is_semantic_fail_status = event.status == "semantic_fail"

        if insp is None and not has_semantic_check_failure and not is_semantic_fail_status:
            continue
        has_any_failure = (
            (
                insp is not None
                and (
                    insp.is_silent_failure
                    or insp.has_tool_failure
                    or insp.tool_failures
                    or insp.semantic_signals
                )
            )
            or has_semantic_check_failure
            or is_semantic_fail_status
        )
        if not has_any_failure:
            continue

        bad_fields = set()
        if insp is not None:
            bad_fields = set(insp.missing_fields + insp.empty_fields)
        # Remove fields that were actually provided elsewhere — parallel siblings
        real_bad = bad_fields - all_provided
        if (
            real_bad
            or seen_bad_fields.intersection(real_bad)
            or (insp is not None and insp.is_silent_failure)
            or (insp is not None and insp.has_tool_failure)
            or (insp is not None and insp.tool_failures)
            or has_semantic_check_failure
            or is_semantic_fail_status
        ):
            event_key = (event.node_name, event.attempt_index)
            if event_key not in seen_nodes:
                chain.append(event.node_name)
                seen_nodes.add(event_key)
            seen_bad_fields.update(real_bad)

    chain.reverse()
    return chain
