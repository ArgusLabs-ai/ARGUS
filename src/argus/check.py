"""CI gate: decide whether a recorded ARGUS run is clean enough to pass a build.

Used by ``argus check`` and the pytest ``--argus`` plugin. A run fails the
gate when the overall status is not ``clean``, or when any node shows a
crash, silent failure, missing fields, tool failure, or semantic fail.

With ``strict="warn_as_fail"``, warning-severity tool failures (e.g. HTTP 429
rate limits) also fail the gate — see PRD US-1.4 / issue #73. Default
``critical_only`` preserves today's behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from argus.models import NodeEvent, RunRecord

# Overall statuses that mean the pipeline was not clean (CI should fail).
UNCLEAN_OVERALL_STATUSES = frozenset(
    {
        "crashed",
        "silent_failure",
        "semantic_fail",
        "interrupted",
    }
)

# Node statuses that mean that step was not clean.
UNCLEAN_NODE_STATUSES = frozenset(
    {
        "fail",
        "crashed",
        "semantic_fail",
    }
)

StrictMode = Literal["critical_only", "warn_as_fail"]
STRICT_CHOICES: tuple[str, ...] = ("critical_only", "warn_as_fail")


@dataclass(frozen=True)
class CheckResult:
    """Outcome of evaluating one ``RunRecord`` as a CI gate."""

    run_id: str
    overall_status: str
    passed: bool
    failing_nodes: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def summary(self) -> str:
        if self.passed:
            return f"{self.run_id}  clean"
        detail = self.reasons[0] if self.reasons else self.overall_status
        nodes = ", ".join(self.failing_nodes) if self.failing_nodes else "—"
        return f"{self.run_id}  {self.overall_status}  ({detail}; nodes: {nodes})"


def _has_tool_warnings(event: NodeEvent) -> bool:
    insp = event.inspection
    if insp is None:
        return False
    if insp.has_tool_warnings:
        return True
    # Backfill for older run JSON that pre-dates has_tool_warnings.
    return any(tf.severity == "warning" for tf in insp.tool_failures)


def _node_reasons(event: NodeEvent, *, strict: StrictMode) -> list[str]:
    reasons: list[str] = []
    name = event.node_name
    if event.status in UNCLEAN_NODE_STATUSES:
        reasons.append(f"{name}: {event.status}")
    insp = event.inspection
    if insp is None:
        return reasons
    if insp.is_silent_failure:
        reasons.append(f"{name}: silent_failure")
    if insp.has_tool_failure:
        reasons.append(f"{name}: tool_failure")
    if strict == "warn_as_fail" and _has_tool_warnings(event):
        reasons.append(f"{name}: tool_warning")
    if insp.missing_fields:
        fields = ", ".join(insp.missing_fields)
        reasons.append(f"{name}: missing_fields ({fields})")
    return reasons


def evaluate_run(
    record: RunRecord,
    *,
    strict: StrictMode = "critical_only",
) -> CheckResult:
    """Return whether ``record`` should pass a CI / pytest gate.

    ``strict="critical_only"`` (default): warning-severity tool failures
    (rate limits, etc.) do not fail the gate — matching recorded run status.

    ``strict="warn_as_fail"``: any warning-severity tool failure also fails
    the gate, even when ``overall_status`` is ``clean``.
    """
    if strict not in STRICT_CHOICES:
        choices = ", ".join(STRICT_CHOICES)
        raise ValueError(f"unknown strict mode {strict!r}; choose from {choices}")

    reasons: list[str] = []
    failing_nodes: list[str] = []
    seen_nodes: set[str] = set()

    if record.overall_status in UNCLEAN_OVERALL_STATUSES:
        reasons.append(f"overall_status={record.overall_status}")

    for event in record.steps:
        if event.status in ("retried", "skipped"):
            continue
        node_reasons = _node_reasons(event, strict=strict)
        if not node_reasons:
            continue
        reasons.extend(node_reasons)
        if event.node_name not in seen_nodes:
            failing_nodes.append(event.node_name)
            seen_nodes.add(event.node_name)

    passed = record.overall_status == "clean" and not failing_nodes
    return CheckResult(
        run_id=record.run_id,
        overall_status=record.overall_status,
        passed=passed,
        failing_nodes=tuple(failing_nodes),
        reasons=tuple(reasons),
    )


def is_run_clean(record: RunRecord, *, strict: StrictMode = "critical_only") -> bool:
    """True when the run should pass ``argus check`` / ``pytest --argus``."""
    return evaluate_run(record, strict=strict).passed
