"""Findings: the normalized per-run failure list, plus the short terminal
summary printed after invoke() (silent on clean runs, VAR-110)."""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING

from argus.models import Finding
from argus.tool_chain_analyzer import detect_recovered_after_transient

if TYPE_CHECKING:
    from argus.models import JudgeScore, NodeEvent, RunRecord, ToolChainFinding

_QUIET_VALUES = frozenset({"1", "true", "yes", "on"})
_SKIP_STATUSES = frozenset({"retried", "skipped", "pass"})
_FAIL_STATUSES = frozenset({"fail", "crashed", "semantic_fail", "degraded_input", "interrupted"})


def _quiet() -> bool:
    return os.environ.get("ARGUS_QUIET", "").strip().lower() in _QUIET_VALUES


def _short_run_id(run_id: str) -> str:
    """Prefer the unique tail of ``YYYYMMDD-HHMMSS-hex`` ids; else first 8 chars."""
    parts = run_id.rsplit("-", 1)
    if len(parts) == 2 and 6 <= len(parts[1]) <= 12 and parts[1].isalnum():
        return parts[1][:8]
    return run_id[:8] if len(run_id) > 8 else run_id


def _failing_event(record: RunRecord) -> NodeEvent | None:
    name = record.first_failure_step or record.interrupt_node
    if name:
        for event in record.steps:
            if event.node_name == name and event.status not in _SKIP_STATUSES:
                return event
    for event in record.steps:
        if event.status in _FAIL_STATUSES:
            return event
    return None


def _dropped_by(record: RunRecord, event: NodeEvent, fail_node: str | None) -> str | None:
    insp = event.inspection
    if insp is not None and insp.degraded_upstream_node:
        culprit = insp.degraded_upstream_node
        if culprit != fail_node:
            return culprit
    if record.root_cause_chain:
        culprit = record.root_cause_chain[0]
        if culprit != fail_node:
            return culprit
    return None


def _detail_line(record: RunRecord, event: NodeEvent | None, fail_node: str | None) -> str | None:
    if event is None:
        return None

    insp = event.inspection
    if insp is not None and insp.missing_fields:
        fields = ", ".join(insp.missing_fields)
        culprit = _dropped_by(record, event, fail_node)
        if culprit:
            return f"missing: {fields}  (dropped by {culprit})"
        return f"missing: {fields}"

    if insp is not None and insp.empty_fields:
        return f"empty: {', '.join(insp.empty_fields)}"

    if insp is not None and insp.tool_failures:
        tf = insp.tool_failures[0]
        name = tf.field_name or tf.failure_type
        return f"{tf.failure_type} on {name}" if tf.field_name else tf.failure_type

    if event.exception:
        first = event.exception.strip().splitlines()[-1].strip()
        if first:
            return first[:120]

    if insp is not None and insp.message and insp.message != "All checks passed":
        return insp.message[:120]

    if event.status == "semantic_fail":
        return "semantic_fail"

    return None


def format_run_finding(record: RunRecord) -> str | None:
    """Return a 2–3 line terminal summary, or ``None`` when the run is clean."""
    if record.overall_status == "clean":
        return None

    short_id = _short_run_id(record.run_id)
    fail_node = record.first_failure_step or record.interrupt_node
    header = f"[argus] run {short_id}  {record.overall_status}"
    if fail_node:
        header += f" on {fail_node}"

    lines = [header]
    detail = _detail_line(record, _failing_event(record), fail_node)
    if detail:
        lines.append(f"        {detail}")
    lines.append("        argus show last   |  argus ui")
    return "\n".join(lines)


def print_run_finding(record: RunRecord) -> None:
    """Print to stderr when something is wrong. No-op on clean runs or ARGUS_QUIET."""
    if _quiet():
        return
    text = format_run_finding(record)
    if text:
        print(text, file=sys.stderr)  # noqa: T201


# ── Normalized findings list ──────────────────────────────────────────────────

_INACTIVE = frozenset({"retried", "skipped"})


def _fid(type_: str, node: str, field_path: str | None, source: str) -> str:
    raw = f"{type_}|{node}|{field_path or ''}|{source}".encode()
    return hashlib.sha1(raw).hexdigest()[:10]


def _mk(
    *,
    node: str,
    type_: str,
    severity: str,
    reason: str,
    source: str,
    field_path: str | None = None,
    origin_node: str | None = None,
    confidence: float | None = None,
    failure_class: str | None = None,
) -> Finding:
    return Finding(
        id=_fid(type_, node, field_path, source),
        node=node,
        type=type_,
        severity=severity,
        reason=reason,
        source=source,  # type: ignore[arg-type]
        field_path=field_path,
        origin_node=origin_node,
        confidence=confidence,
        failure_class=failure_class,
    )


def _event_findings(event: NodeEvent) -> list[Finding]:
    out: list[Finding] = []
    name = event.node_name
    insp = event.inspection
    origin = insp.degraded_upstream_node if insp is not None else None

    if event.status == "crashed":
        exc = (event.exception or "").strip().splitlines()
        head = exc[-1] if exc else "an exception"
        out.append(
            _mk(
                node=name,
                type_="crash",
                severity="critical",
                reason=f"Node `{name}` raised {head}.",
                source="crash",
                origin_node=origin,
            )
        )

    if event.status == "degraded_input" and insp is not None:
        fields = ", ".join(f"`{f}`" for f in insp.degraded_fields) or "its input"
        who = f"`{origin}`" if origin else "an upstream node"
        out.append(
            _mk(
                node=name,
                type_="degraded_input",
                severity="critical",
                reason=f"Node `{name}` consumed {fields} that {who} had dropped or degraded.",
                source="heuristic",
                field_path=insp.degraded_fields[0] if insp.degraded_fields else None,
                origin_node=origin,
            )
        )

    if insp is not None:
        for f in insp.missing_fields:
            who = f"`{origin}`" if origin else f"`{name}`"
            # The contextual layer and the crash walk both write a fuller
            # sentence onto the inspection — it names the *reader* that needed
            # the field, which is the half of the story this generic line
            # cannot reconstruct. Prefer it when it is about this field.
            authored = (insp.message or "").strip()
            reason = (
                authored
                if authored.startswith(f"Field `{f}`")
                else f"Field `{f}` required downstream was not set by {who}."
            )
            out.append(
                _mk(
                    node=name,
                    type_="missing_field",
                    severity="critical",
                    reason=reason,
                    source="heuristic",
                    field_path=f,
                    # The finding is filed *on* the origin, so when no separate
                    # upstream node is implicated this node is the origin.
                    origin_node=origin or name,
                )
            )
        for m in insp.type_mismatches:
            out.append(
                _mk(
                    node=name,
                    type_="type_mismatch",
                    severity="critical",
                    reason=(
                        f"Field `{m.field_name}` from `{name}` is {m.actual_type}; "
                        f"the next node expects {m.expected_type}."
                    ),
                    source="heuristic",
                    field_path=m.field_name,
                )
            )
        for tf in insp.tool_failures:
            out.append(
                _mk(
                    node=name,
                    type_=tf.failure_type,
                    severity=tf.severity,
                    reason=f"Node `{name}` output field `{tf.field_name}` shows {tf.evidence}.",
                    source="heuristic",
                    field_path=tf.field_name,
                    # PRD-02: lets `argus check --format json` sort "retry the
                    # run" from "fix the code" without reading the evidence.
                    failure_class=tf.failure_class,
                )
            )
        for sig in insp.semantic_signals:
            path = sig.dotted_path or None
            where = f" at `{path}`" if path else ""
            custom = sig.category == "custom_check"
            out.append(
                _mk(
                    node=name,
                    type_=sig.category,
                    severity=sig.severity,
                    reason=f"Node `{name}` output{where} matched {sig.sig_id}: {sig.description}.",
                    # A custom check is an LLM verdict, not a pattern match.
                    source="llm" if custom else "heuristic",
                    # A custom check has no field; its spec is what tells two
                    # signals on one node apart, and collect_findings de-dupes
                    # by id (as it does for validators, keyed by validator name).
                    field_path=sig.sig_id if custom else path,
                    confidence=sig.confidence,
                )
            )
        if insp.is_silent_failure and not insp.missing_fields and not insp.tool_failures:
            # Silent failure with no more specific carrier (e.g. empty_output).
            out.append(
                _mk(
                    node=name,
                    type_="silent_failure",
                    severity=insp.severity if insp.severity != "ok" else "critical",
                    reason=f"Node `{name}` silently failed: {insp.message}",
                    source="heuristic",
                    origin_node=origin,
                )
            )

    for vr in event.validator_results:
        if vr.is_valid:
            continue
        out.append(
            _mk(
                node=name,
                type_="validator",
                severity=vr.severity,
                reason=f"Validator `{vr.validator_name}` rejected `{name}`: {vr.message}",
                source="validator",
                field_path=vr.validator_name,
            )
        )

    for an in event.anomaly_signals:
        out.append(
            _mk(
                node=name,
                type_=an.anomaly_id,
                severity=an.severity,
                reason=f"Node `{name}` behaved unexpectedly: {an.reason}",
                source="anomaly",
                field_path=an.field_path or None,
                confidence=an.suspicion_score,
            )
        )

    sc = event.semantic_check
    if sc is not None and sc.evaluated and not sc.passed:
        # The judge's opinion is critical only when it moved the status.
        # It no longer originates a fail, so this is almost always a warning
        # kept so `argus show` can display the review.
        stands = event.status == "semantic_fail"
        out.append(
            _mk(
                node=name,
                type_="semantic_fail",
                severity="critical" if stands else "warning",
                reason=(
                    f"LLM judge failed `{name}`: {sc.reason}"
                    if stands
                    else f"LLM judge doubted `{name}` (no rule agreed; not gating): {sc.reason}"
                ),
                source="llm",
                confidence=sc.confidence,
            )
        )

    for item in event.review:
        role = item.get("role", "advisory")
        lead = {
            "promoted": "a rule warned and the run reviewer verified it",
            "confirms": "the run reviewer verified it too",
            "two_models": "two models verified it independently; no rule flagged this step",
        }.get(role, "advisory, not gating; no rule flagged this step")
        fix = f" Should be: {item['correction']}." if item.get("correction") else ""
        out.append(
            _mk(
                node=name,
                type_=f"review_{item.get('kind') or 'finding'}",
                severity="critical" if role in ("promoted", "two_models") else "warning",
                reason=f"Node `{name}` ({lead}): {item.get('why') or item.get('claim')}{fix}",
                source="llm",
            )
        )
    return out


def collect_findings(
    steps: list[NodeEvent],
    tool_chain_findings: list[ToolChainFinding] | None = None,
    judge_scores: list[JudgeScore] | None = None,
) -> list[Finding]:
    """Flatten every failure signal on a run into one ordered, de-duplicated list.

    Retried and skipped steps are excluded (they do not count against the run —
    see docs/STATUS.md). Order: critical before warning before info, then step
    order. Ids are content hashes, stable across runs of the same graph.

    A ``judge_scores`` row under its floor (``severity == "warning"``, decided
    when the run was recorded) is a ``low_judge_score`` warning on its node. It
    stands in for the generic ``validator`` finding the same invocation would
    otherwise file, and its id includes the step, so one judge on one node at
    two steps keeps both. Advisory: a finding only, never a status or a gate.

    Recovered-after-transient (PRD-02): a node that passed after its retried
    steps absorbed >=2 transient or rate-limited tool failures gets an info
    ``recovered_after_transient`` finding. It annotates a clean run that hid a
    flaky dependency and never gates.
    """
    raw: list[tuple[int, Finding]] = []
    inactive = {
        ev.step_index for ev in steps if ev.status in _INACTIVE and ev.step_index is not None
    }
    low = [js for js in judge_scores or [] if js.severity == "warning"]
    low = [js for js in low if js.step_index not in inactive]
    judged = {(js.step_index, js.validator_name) for js in low}
    for ev in steps:
        if ev.status in _INACTIVE:
            continue
        raw.extend(
            (ev.step_index, f)
            for f in _event_findings(ev)
            if not (f.type == "validator" and (ev.step_index, f.field_path) in judged)
        )
    for js in low:
        node = js.origin_node or "*"
        said = ": ".join(t for t in (js.label, js.explanation) if t)
        raw.append(
            (
                10**9 if js.step_index is None else js.step_index,
                _mk(
                    node=node,
                    type_="low_judge_score",
                    severity="warning",
                    reason=(
                        f"Judge `{js.validator_name}` scored `{node}` {js.score:.2f} "
                        f"({js.direction}), under the floor" + (f": {said}" if said else "")
                    ),
                    source="validator",
                    field_path=f"{js.validator_name}@{js.step_index}",
                    origin_node=js.origin_node,
                ),
            )
        )
    for tc in tool_chain_findings or []:
        node = tc.nodes_involved[0] if tc.nodes_involved else "*"
        raw.append(
            (
                10**9,
                _mk(
                    node=node,
                    type_=tc.finding_type,
                    severity=tc.severity,
                    reason=f"Across {', '.join(tc.nodes_involved) or 'the run'}: {tc.description}",
                    source="heuristic",
                    confidence=tc.confidence,
                ),
            )
        )

    for node, absorbed in detect_recovered_after_transient(steps):
        raw.append(
            (
                10**9,
                _mk(
                    node=node,
                    type_="recovered_after_transient",
                    severity="info",
                    reason=(
                        f"Node `{node}` absorbed {absorbed} transient failures across retried "
                        "steps before it passed; the run is clean, but a dependency looks flaky."
                    ),
                    source="heuristic",
                    origin_node=node,
                ),
            )
        )

    rank = {"critical": 0, "warning": 1, "info": 2}
    seen: set[str] = set()
    out: list[Finding] = []
    for _, f in sorted(raw, key=lambda t: (rank.get(t[1].severity, 3), t[0])):
        if f.id in seen:
            continue
        seen.add(f.id)
        out.append(f)
    return out
