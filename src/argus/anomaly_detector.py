"""Behavioral anomaly detection for ARGUS.

Runs AFTER the heuristic signature detector. Does not attempt semantic
understanding — estimates suspiciousness from output shape, token density,
length distributions, and structural characteristics.

Supports 3-level behavior context:
  1. Auto-inferred from output shape
  2. Pipeline-level default (ArgusSession parameter)
  3. Per-node override (ArgusSession parameter)
"""

from __future__ import annotations

import json
import re
from typing import Any

from argus.models import AnomalySignal, BehaviorConfig

# ── Behavior type profiles ───────────────────────────────────────────────────

BEHAVIOR_PROFILES: dict[str, dict[str, Any]] = {
    "structured_json": {
        "min_keys": 1,
        "expects_nested": True,
        "min_info_density": 0.15,
        "length_range": (20, 50_000),
    },
    "retrieval_result": {
        "min_keys": 1,
        "expects_list_field": True,
        "min_info_density": 0.20,
        "length_range": (50, 100_000),
    },
    "classification": {
        "min_keys": 1,
        "expects_nested": False,
        "min_info_density": 0.30,
        "length_range": (5, 500),
    },
    "detailed_text": {
        "min_keys": 1,
        "expects_nested": False,
        "min_info_density": 0.25,
        "length_range": (100, 50_000),
    },
    "tool_output": {
        "min_keys": 1,
        "expects_nested": False,
        "min_info_density": 0.10,
        "length_range": (10, 100_000),
    },
    "reasoning_chain": {
        "min_keys": 1,
        "expects_nested": True,
        "min_info_density": 0.20,
        "length_range": (200, 100_000),
        "expects_steps": True,
    },
    "chat_response": {
        "min_keys": 1,
        "expects_nested": False,
        "min_info_density": 0.30,
        "length_range": (50, 5_000),
    },
    "code_generation": {
        "min_keys": 1,
        "expects_nested": False,
        "min_info_density": 0.10,
        "length_range": (10, 100_000),
    },
}

_RETRIEVAL_KEYS = re.compile(
    r"(results?|items?|documents?|records?|rows?|hits?|entries?|matches?|findings?)$",
    re.IGNORECASE,
)
_TOOL_KEYS = re.compile(r"(status|result|response|data|output)$", re.IGNORECASE)
_STEP_KEYS = re.compile(r"(steps?|chain|reasoning|thought|trace)", re.IGNORECASE)

_GENERIC_PHRASES = frozenset(
    [
        "i'm sorry",
        "i cannot",
        "i can't",
        "as an ai",
        "i don't have",
        "i am unable",
        "not available",
        "no data",
        "no information",
        "please provide",
        "try again",
        "something went wrong",
        "an error occurred",
        "unable to process",
        "no results found",
        "nothing to display",
    ]
)

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_all_strings(obj: Any, path: str = "") -> list[tuple[str, str]]:
    """Recursively extract all string values with their dotted paths."""
    results: list[tuple[str, str]] = []
    if isinstance(obj, str):
        results.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else k
            results.extend(_extract_all_strings(v, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child_path = f"{path}[{i}]"
            results.extend(_extract_all_strings(item, child_path))
    return results


def _tokenize(text: str) -> list[str]:
    """Simple word tokenizer."""
    return _WORD_RE.findall(text.lower())


def _measure_depth(obj: Any, current: int = 0) -> int:
    """Max nesting depth of a dict/list structure."""
    if isinstance(obj, dict):
        if not obj:
            return current + 1
        return max(_measure_depth(v, current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return current + 1
        return max(_measure_depth(item, current + 1) for item in obj)
    return current


def _serialize_length(obj: Any) -> int:
    """Total character length of JSON-serialized output."""
    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return 0


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    # Empty dicts are suspicious, but empty lists are valid — they mean
    # "nothing found" which is a legitimate result (e.g. no vulnerabilities,
    # no errors, no matches). Only flag empty dicts.
    if isinstance(value, dict) and len(value) == 0:
        return True
    return False


# ── Auto-inference ───────────────────────────────────────────────────────────


def infer_behavior_type(output_dict: dict[str, Any]) -> str:
    """Infer expected behavior type from output shape."""
    if not output_dict:
        return "structured_json"

    # Check for retrieval-like: list fields with dict items + retrieval keys
    for key, value in output_dict.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if _RETRIEVAL_KEYS.search(key):
                return "retrieval_result"

    # Check for classification: single short string value
    string_values = [v for v in output_dict.values() if isinstance(v, str)]
    if len(output_dict) <= 2 and string_values:
        max_len = max(len(s) for s in string_values)
        if max_len < 100 and len(string_values) == len(output_dict):
            return "classification"

    # Check for reasoning chain: deep nesting + step-like keys
    depth = _measure_depth(output_dict)
    has_step_keys = any(_STEP_KEYS.search(k) for k in output_dict)
    if depth >= 3 and has_step_keys:
        return "reasoning_chain"

    # Check for tool output: status/result/response keys
    tool_key_count = sum(1 for k in output_dict if _TOOL_KEYS.search(k))
    if tool_key_count >= 1 and len(output_dict) <= 4:
        return "tool_output"

    # Check for detailed text: long string values
    all_strings = _extract_all_strings(output_dict)
    if all_strings:
        longest = max(len(s) for _, s in all_strings)
        if longest > 200:
            return "detailed_text"

    # Check for structured json: multiple keys or nested dicts
    if len(output_dict) >= 2 or any(isinstance(v, dict) for v in output_dict.values()):
        return "structured_json"

    return "structured_json"


# ── Behavior type resolution ────────────────────────────────────────────────


def resolve_behavior_type(
    node_name: str,
    output_dict: dict[str, Any],
    config: BehaviorConfig | None,
) -> str:
    """Resolve behavior type: node override > pipeline default > auto-inferred."""
    if config and node_name in config.node_behaviors:
        return config.node_behaviors[node_name]
    if config and config.default_behavior_type:
        return config.default_behavior_type
    return infer_behavior_type(output_dict)


# ── Anomaly checks ──────────────────────────────────────────────────────────


def _check_length_collapse(
    output_dict: dict[str, Any],
    profile: dict[str, Any],
    behavior_type: str,
) -> AnomalySignal | None:
    """BA-001: Drastic output length deviation."""
    length = _serialize_length(output_dict)
    lo, hi = profile.get("length_range", (10, 100_000))

    if length < lo * 0.3:
        distance = 1.0 - (length / (lo * 0.3)) if lo > 0 else 1.0
        score = min(1.0, max(0.0, distance))
        severity = "critical" if score > 0.7 else "warning"
        return AnomalySignal(
            anomaly_id="BA-001",
            severity=severity,
            suspicion_score=round(score, 2),
            reason="output length collapse",
            expected_behavior=f"{lo}–{hi} chars for {behavior_type}",
            observed_behavior=f"{length} chars total",
            field_path="",
        )

    if length > hi * 3:
        distance = (length - hi * 3) / (hi * 3) if hi > 0 else 1.0
        score = min(1.0, max(0.0, distance))
        severity = "warning"
        return AnomalySignal(
            anomaly_id="BA-001",
            severity=severity,
            suspicion_score=round(score, 2),
            reason="output length explosion",
            expected_behavior=f"{lo}–{hi} chars for {behavior_type}",
            observed_behavior=f"{length} chars total",
            field_path="",
        )

    return None


def _check_repetitive_filler(output_dict: dict[str, Any]) -> AnomalySignal | None:
    """BA-002: Repeated tokens or phrases (statistical, not pattern-matched)."""
    all_strings = _extract_all_strings(output_dict)
    if not all_strings:
        return None

    all_text = " ".join(s for _, s in all_strings)
    tokens = _tokenize(all_text)
    if len(tokens) < 10:
        return None

    # Check bigram and trigram repetition
    worst_ratio = 0.0
    worst_path = ""
    for path, text in all_strings:
        words = _tokenize(text)
        if len(words) < 8:
            continue
        counts: dict[str, int] = {}
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                gram = " ".join(words[i : i + n])
                counts[gram] = counts.get(gram, 0) + 1
        if not counts:
            continue
        max_count = max(counts.values())
        if max_count >= 4:
            ratio = max_count / len(words)
            if ratio > worst_ratio:
                worst_ratio = ratio
                worst_path = path

    if worst_ratio < 0.15:
        return None

    score = min(1.0, worst_ratio * 2)
    severity = "critical" if score > 0.7 else "warning"
    return AnomalySignal(
        anomaly_id="BA-002",
        severity=severity,
        suspicion_score=round(score, 2),
        reason="repetitive filler detected",
        expected_behavior="diverse, non-repetitive content",
        observed_behavior=f"{worst_ratio:.0%} token repetition ratio",
        field_path=worst_path,
    )


def _check_info_density(
    output_dict: dict[str, Any],
    profile: dict[str, Any],
    behavior_type: str,
) -> AnomalySignal | None:
    """BA-003: Unusually low information density."""
    all_strings = _extract_all_strings(output_dict)
    if not all_strings:
        return None

    all_text = " ".join(s for _, s in all_strings)
    tokens = _tokenize(all_text)
    if len(tokens) < 5:
        return None

    unique = len(set(tokens))
    density = unique / len(tokens)
    threshold = profile.get("min_info_density", 0.15)

    if density >= threshold:
        return None

    distance = (threshold - density) / threshold
    score = min(1.0, max(0.0, distance))
    severity = "critical" if score > 0.7 else "warning"
    return AnomalySignal(
        anomaly_id="BA-003",
        severity=severity,
        suspicion_score=round(score, 2),
        reason="low information density",
        expected_behavior=f"≥{threshold:.0%} unique token ratio for {behavior_type}",
        observed_behavior=f"{density:.0%} unique tokens ({unique}/{len(tokens)})",
        field_path="",
    )


def _check_generic_response(output_dict: dict[str, Any]) -> AnomalySignal | None:
    """BA-004: Suspiciously generic responses."""
    all_strings = _extract_all_strings(output_dict)
    if not all_strings:
        return None

    generic_hits = 0
    total_checked = 0
    worst_path = ""

    for path, text in all_strings:
        lower = text.lower().strip()
        if len(lower) < 5:
            continue
        total_checked += 1
        for phrase in _GENERIC_PHRASES:
            if phrase in lower:
                generic_hits += 1
                worst_path = path
                break

    if total_checked == 0 or generic_hits == 0:
        return None

    ratio = generic_hits / total_checked
    if ratio < 0.3:
        return None

    score = min(1.0, ratio)
    severity = "critical" if score > 0.7 else "warning"
    return AnomalySignal(
        anomaly_id="BA-004",
        severity=severity,
        suspicion_score=round(score, 2),
        reason="suspiciously generic response",
        expected_behavior="specific, contextual content",
        observed_behavior=f"{generic_hits}/{total_checked} fields contain generic phrases",
        field_path=worst_path,
    )


def _check_structural_malformation(
    output_dict: dict[str, Any],
    profile: dict[str, Any],
    behavior_type: str,
) -> AnomalySignal | None:
    """BA-005: Structurally malformed outputs."""
    signals: list[tuple[float, str]] = []

    # Check nested expectation
    if profile.get("expects_nested", False):
        depth = _measure_depth(output_dict)
        if depth < 2:
            signals.append((0.6, "expected nested structure, got flat output"))

    # Check list field expectation
    if profile.get("expects_list_field", False):
        has_list = any(isinstance(v, list) for v in output_dict.values())
        if not has_list:
            signals.append((0.7, "expected list field, none found"))

    # Check step expectation (reasoning chain)
    if profile.get("expects_steps", False):
        has_steps = any(
            _STEP_KEYS.search(k) and isinstance(v, (list, dict)) for k, v in output_dict.items()
        )
        if not has_steps:
            signals.append((0.5, "expected step/chain structure, not found"))

    # Check min keys
    min_keys = profile.get("min_keys", 1)
    if len(output_dict) < min_keys:
        signals.append((0.4, f"expected ≥{min_keys} keys, got {len(output_dict)}"))

    if not signals:
        return None

    worst_score, worst_reason = max(signals, key=lambda x: x[0])
    severity = "critical" if worst_score > 0.7 else "warning"
    return AnomalySignal(
        anomaly_id="BA-005",
        severity=severity,
        suspicion_score=round(worst_score, 2),
        reason="structural malformation",
        expected_behavior=f"{behavior_type} structure profile",
        observed_behavior=worst_reason,
        field_path="",
    )


def _check_shallow_empty(
    output_dict: dict[str, Any],
    profile: dict[str, Any],
    behavior_type: str,
    input_state: dict[str, Any] | None = None,
) -> AnomalySignal | None:
    """BA-006: Unusually empty or shallow outputs."""
    if not output_dict:
        # ponytail: LangGraph terminal/formatting nodes often emit no new
        # state keys — they read from accumulated messages in input_state.
        # Empty output with non-empty input is a pass-through, not a failure.
        if input_state:
            return None
        return AnomalySignal(
            anomaly_id="BA-006",
            severity="critical",
            suspicion_score=1.0,
            reason="completely empty output",
            expected_behavior=f"non-empty output for {behavior_type}",
            observed_behavior="empty dict",
            field_path="",
        )

    total = len(output_dict)
    empty_count = sum(1 for v in output_dict.values() if _is_empty(v))
    ratio = empty_count / total

    if ratio < 0.5:
        return None

    score = min(1.0, ratio)
    severity = "critical" if score > 0.8 else "warning"
    empty_keys = [k for k, v in output_dict.items() if _is_empty(v)]
    return AnomalySignal(
        anomaly_id="BA-006",
        severity=severity,
        suspicion_score=round(score, 2),
        reason="shallow/empty output",
        expected_behavior=f"substantive output for {behavior_type}",
        observed_behavior=f"{empty_count}/{total} fields empty ({', '.join(empty_keys[:3])})",
        field_path="",
    )


def _check_incomplete_reasoning(
    output_dict: dict[str, Any],
    behavior_type: str,
) -> AnomalySignal | None:
    """BA-007: Incomplete reasoning traces (reasoning_chain only)."""
    if behavior_type != "reasoning_chain":
        return None

    # Look for step-like lists
    step_lists: list[tuple[str, list]] = []
    for k, v in output_dict.items():
        if isinstance(v, list) and _STEP_KEYS.search(k):
            step_lists.append((k, v))

    if not step_lists:
        return None

    for key, steps in step_lists:
        if len(steps) == 1:
            return AnomalySignal(
                anomaly_id="BA-007",
                severity="warning",
                suspicion_score=0.5,
                reason="incomplete reasoning trace",
                expected_behavior="multi-step reasoning chain",
                observed_behavior=f"only 1 step in '{key}'",
                field_path=key,
            )

        # Check for abrupt truncation: last step much shorter than others
        if len(steps) >= 2:
            step_lengths = []
            for s in steps:
                if isinstance(s, str):
                    step_lengths.append(len(s))
                elif isinstance(s, dict):
                    step_lengths.append(_serialize_length(s))
                else:
                    step_lengths.append(0)
            if step_lengths and step_lengths[-1] > 0:
                prior = step_lengths[:-1]
                avg_prior = sum(prior) / len(prior) if prior else step_lengths[0]
                if avg_prior > 0 and step_lengths[-1] < avg_prior * 0.2:
                    last_len = step_lengths[-1]
                    return AnomalySignal(
                        anomaly_id="BA-007",
                        severity="warning",
                        suspicion_score=0.6,
                        reason="truncated reasoning trace",
                        expected_behavior="consistent step lengths",
                        observed_behavior=f"last step {last_len} chars vs avg {avg_prior:.0f}",
                        field_path=key,
                    )

    return None


def _check_abnormal_tool_response(
    output_dict: dict[str, Any],
    behavior_type: str,
) -> AnomalySignal | None:
    """BA-008: Abnormal tool response patterns (tool_output only)."""
    if behavior_type != "tool_output":
        return None

    # Check for suspiciously empty tool response
    all_strings = _extract_all_strings(output_dict)
    total_text_len = sum(len(s) for _, s in all_strings)
    if total_text_len == 0 and len(output_dict) > 0:
        return AnomalySignal(
            anomaly_id="BA-008",
            severity="warning",
            suspicion_score=0.6,
            reason="tool response contains no text content",
            expected_behavior="tool output with meaningful content",
            observed_behavior=f"{len(output_dict)} keys, 0 chars of text",
            field_path="",
        )

    # Check for uniform repetitive list items (same shape repeated)
    for key, value in output_dict.items():
        if not isinstance(value, list) or len(value) < 3:
            continue
        if not all(isinstance(item, dict) for item in value):
            continue
        serialized = [json.dumps(item, sort_keys=True, default=str) for item in value]
        unique_items = len(set(serialized))
        if unique_items == 1 and len(value) >= 3:
            return AnomalySignal(
                anomaly_id="BA-008",
                severity="warning",
                suspicion_score=0.7,
                reason="identical repeated items in tool response",
                expected_behavior="distinct items in list",
                observed_behavior=f"{len(value)} identical items in '{key}'",
                field_path=key,
            )

    return None


# ── Main entry point ─────────────────────────────────────────────────────────


def detect_anomalies(
    node_name: str,
    output_dict: dict[str, Any],
    config: BehaviorConfig | None = None,
    input_state: dict[str, Any] | None = None,
) -> tuple[str, list[AnomalySignal]]:
    """Run all behavioral anomaly checks.

    Returns (behavior_type, anomaly_signals).
    """
    behavior_type = resolve_behavior_type(node_name, output_dict, config)
    profile = BEHAVIOR_PROFILES.get(behavior_type, BEHAVIOR_PROFILES["structured_json"])

    checks = [
        _check_length_collapse(output_dict, profile, behavior_type),
        _check_repetitive_filler(output_dict),
        _check_info_density(output_dict, profile, behavior_type),
        _check_generic_response(output_dict),
        _check_structural_malformation(output_dict, profile, behavior_type),
        _check_shallow_empty(output_dict, profile, behavior_type, input_state),
        _check_incomplete_reasoning(output_dict, behavior_type),
        _check_abnormal_tool_response(output_dict, behavior_type),
    ]

    signals = [s for s in checks if s is not None]
    return behavior_type, signals
