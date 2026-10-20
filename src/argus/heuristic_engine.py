from __future__ import annotations

from typing import Any

from argus.models import SemanticSignal
from argus.registry import get_registry, scan_value

_DEFAULT_MAX_DEPTH = 10
_DEFAULT_MAX_FIELDS = 100
_DEFAULT_MAX_VALUE_LEN = 10_000
_DEPTH_LIMIT_SIG_ID = "DEPTH-LIMIT"
DEPTH_LIMIT_DESCRIPTION = "max scan depth reached — subtree not scanned"


class HeuristicEngine:
    """Recursive, data-driven semantic failure scanner.

    Framework-agnostic: operates on any Python dict/list/str structure.
    All SDK-specific outputs should be normalized before passing here.

    The engine loads the signature registry once at construction time and
    reuses it across all scan calls. Instances are lightweight and can be
    created per-call or shared across calls.
    """

    def __init__(
        self,
        registry: list[dict[str, Any]] | None = None,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        max_fields: int = _DEFAULT_MAX_FIELDS,
        max_value_len: int = _DEFAULT_MAX_VALUE_LEN,
    ) -> None:
        self._registry = registry if registry is not None else get_registry()
        self._max_depth = max_depth
        self._max_fields = max_fields
        self._max_value_len = max_value_len
        self._field_count = 0

    def scan(self, value: Any, root_key: str = "output") -> list[SemanticSignal]:
        """Recursively scan value starting at root_key as the first path segment."""
        self._field_count = 0
        seen: set[tuple[str, tuple[str, ...]]] = set()
        signals: list[SemanticSignal] = []
        self._scan_node(value, path=(root_key,), depth=0, signals=signals, seen=seen)
        return signals

    def _scan_node(
        self,
        value: Any,
        path: tuple[str, ...],
        depth: int,
        signals: list[SemanticSignal],
        seen: set[tuple[str, tuple[str, ...]]],
    ) -> None:
        if depth >= self._max_depth:
            dedup_key = (_DEPTH_LIMIT_SIG_ID, path)
            if dedup_key not in seen:
                seen.add(dedup_key)
                signals.append(
                    SemanticSignal(
                        sig_id=_DEPTH_LIMIT_SIG_ID,
                        category="structural_anomaly",
                        severity="warning",
                        description=DEPTH_LIMIT_DESCRIPTION,
                        field_path=path,
                        evidence=f"depth={depth}",
                    )
                )
            return

        if isinstance(value, str):
            if len(value) <= self._max_value_len:
                for match in scan_value(value, self._registry):
                    dedup_key = (match.sig_id, path)
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        signals.append(
                            SemanticSignal(
                                sig_id=match.sig_id,
                                category=match.category,
                                severity=match.severity,
                                description=match.description,
                                field_path=path,
                                evidence=match.evidence,
                                confidence=match.confidence,
                            )
                        )
            return  # strings are leaf nodes — no further descent

        if isinstance(value, dict):
            for k, v in value.items():
                self._field_count += 1
                if self._field_count > self._max_fields:
                    return
                self._scan_node(v, (*path, k), depth + 1, signals, seen)
            return

        if isinstance(value, list):
            for i, item in enumerate(value):
                self._scan_node(item, (*path, f"[{i}]"), depth + 1, signals, seen)
            return

        # int, float, bool, None — not string-scannable, skip silently


def scan_execution_output(
    output_dict: dict[str, Any],
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> list[SemanticSignal]:
    """Recursively scan a top-level execution output dict for semantic failures.

    Each top-level key becomes the first path segment, producing paths like
    ("result", "items", "[0]", "summary") with no artificial wrapper key.

    This is the primary entry point for framework-agnostic semantic scanning.
    Normalize SDK-specific outputs to a plain dict before calling this.
    """
    engine = HeuristicEngine(max_depth=max_depth)
    engine._field_count = 0
    seen: set[tuple[str, tuple[str, ...]]] = set()
    signals: list[SemanticSignal] = []
    for key, value in output_dict.items():
        engine._field_count += 1
        if engine._field_count > engine._max_fields:
            break
        engine._scan_node(value, path=(key,), depth=0, signals=signals, seen=seen)
    return signals
