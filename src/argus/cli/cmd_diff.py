from __future__ import annotations

from itertools import zip_longest
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.rule import Rule
from rich.text import Text

from argus import determinism
from argus.cli import print_footer
from argus.findings import _INACTIVE
from argus.models import InspectionResult, JudgeScore, NodeEvent, RunRecord, ValidatorResult
from argus.storage import load_run

console = Console()

_STATUS_STYLE = {
    "pass": "bold green",
    "fail": "bold yellow",
    "crashed": "bold red",
    "semantic_fail": "bold magenta",
    "interrupted": "bold yellow",
    "degraded_input": "bold yellow",
    "skipped": "dim",
    "retried": "dim",
}

_STATUS_LABEL = {
    "pass": "pass",
    "fail": "silent failure",
    "crashed": "crashed",
    "semantic_fail": "semantic fail",
    "interrupted": "interrupted",
    "degraded_input": "⚠ degraded",
    "skipped": "⊘ skipped",
    "retried": "↻ retried",
}

_OVERALL_STYLE = {
    "clean": "bold green",
    "silent_failure": "bold yellow",
    "crashed": "bold red",
    "semantic_fail": "bold magenta",
    "interrupted": "bold yellow",
}

_DURATION_THRESHOLD_MS = 100  # suppress duration diff when abs(delta) < this


def diff_runs(run_id_a: str, run_id_b: str | None = None) -> None:
    """Compare two runs node-by-node and print a diff."""
    try:
        run_a = load_run(run_id_a)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        return

    if run_id_b is None:
        if run_a.parent_run_id is None:
            console.print(
                "[red]Error:[/red] Only one run ID given and this run has no parent.\n"
                "  Usage: argus diff <before> <after>"
            )
            return
        try:
            before = load_run(run_a.parent_run_id)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]Error:[/red] Could not load parent run: {e}")
            return
        after = run_a
    else:
        before = run_a
        try:
            after = load_run(run_id_b)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]Error:[/red] {e}")
            return

    before_map = _build_node_map(before)
    after_map = _build_node_map(after)
    all_names = _ordered_node_names(before.steps, after.steps)

    if not all_names:
        console.print("[dim]Both runs have no recorded steps.[/dim]")
        return

    name_col = max(len(n) for n in all_names) + 2

    console.print()
    _print_header(before, after)
    warning = determinism.comparability_warning(before, after)
    if warning:  # a note on the verdicts only: the diff and its outcome are unchanged
        console.print(f"  [yellow]{escape(warning)}[/yellow]")
    console.print()
    console.print(Rule(style="dim"))
    console.print()

    stats: dict[str, int] = {
        "changed": 0,
        "fixed": 0,
        "regressed": 0,
        "new": 0,
        "frozen": 0,
    }

    for name in all_names:
        in_before = name in before_map
        in_after = name in after_map

        if in_before and in_after:
            b_event = before_map[name]
            a_event = after_map[name]
            changed = _print_node_diff(name, b_event, a_event, name_col)
            if changed:
                stats["changed"] += 1
                b_bad = b_event.status in ("crashed", "fail", "semantic_fail", "degraded_input")
                a_bad = a_event.status in ("crashed", "fail", "semantic_fail", "degraded_input")
                if b_bad and a_event.status == "pass":
                    stats["fixed"] += 1
                elif b_event.status == "pass" and a_bad:
                    stats["regressed"] += 1
        elif in_before:
            _print_frozen_node(name, before_map[name], after, name_col)
            stats["frozen"] += 1
        else:
            _print_new_node(name, after_map[name], name_col)
            stats["new"] += 1

    console.print(Rule(style="dim"))
    console.print()
    _print_summary(stats)
    _print_judge_score_deltas(before, after)
    console.print()
    print_footer()


# ── Node map ─────────────────────────────────────────────────────────────────


def _build_node_map(record: RunRecord) -> dict[str, NodeEvent]:
    """Last execution of each node wins (handles cyclic runs)."""
    result: dict[str, NodeEvent] = {}
    for event in record.steps:
        result[event.node_name] = event
    return result


def _ordered_node_names(
    before_steps: list[NodeEvent],
    after_steps: list[NodeEvent],
) -> list[str]:
    """Nodes in before-order first, then any nodes only in after."""
    seen: set[str] = set()
    result: list[str] = []
    for event in before_steps:
        if event.node_name not in seen:
            seen.add(event.node_name)
            result.append(event.node_name)
    for event in after_steps:
        if event.node_name not in seen:
            seen.add(event.node_name)
            result.append(event.node_name)
    return result


# ── Header ───────────────────────────────────────────────────────────────────


def _print_header(before: RunRecord, after: RunRecord) -> None:
    b_started = (before.started_at or "")[:16].replace("T", "  ")
    a_started = (after.started_at or "")[:16].replace("T", "  ")
    b_style = _OVERALL_STYLE.get(before.overall_status, "dim")
    a_style = _OVERALL_STYLE.get(after.overall_status, "dim")

    console.print(Text("  argus diff", style="bold italic"))
    console.print()

    b_line = Text()
    b_line.append("  before  ", style="dim")
    b_line.append(before.run_id[:8], style="dim")
    b_line.append(f"  {b_started}  ", style="dim")
    b_line.append(before.overall_status, style=b_style)
    console.print(b_line)

    a_line = Text()
    a_line.append("  after   ", style="dim")
    a_line.append(after.run_id[:8], style="dim")
    a_line.append(f"  {a_started}  ", style="dim")
    a_line.append(after.overall_status, style=a_style)
    if after.replay_from_step:
        a_line.append(f"  replay from: {after.replay_from_step}", style="italic dim")
    console.print(a_line)


# ── Per-node printers ────────────────────────────────────────────────────────


def _print_node_diff(
    name: str,
    before: NodeEvent,
    after: NodeEvent,
    name_col: int,
) -> bool:
    """Print diff for a node present in both runs. Returns True if anything changed."""
    status_changed = before.status != after.status
    field_diffs = _diff_output(before.output_dict, after.output_dict)
    inspection_diffs = _diff_inspection(before.inspection, after.inspection)
    validator_diffs = _diff_validators(before.validator_results, after.validator_results)
    has_changes = (
        status_changed or bool(field_diffs) or bool(inspection_diffs) or bool(validator_diffs)
    )
    pad = " " * (name_col - len(name))

    if not has_changes:
        style = _STATUS_STYLE.get(after.status, "dim")
        label = _STATUS_LABEL.get(after.status, after.status)
        console.print(
            f"  [bold]{name}[/bold]{pad}  [{style}]{label}[/{style}]  [dim]unchanged[/dim]"
        )
        console.print()
        return False

    if status_changed:
        b_style = _STATUS_STYLE.get(before.status, "dim")
        a_style = _STATUS_STYLE.get(after.status, "dim")
        b_label = _STATUS_LABEL.get(before.status, before.status)
        a_label = _STATUS_LABEL.get(after.status, after.status)
        b_was_bad = before.status in ("crashed", "fail", "semantic_fail", "degraded_input")
        b_was_good = before.status == "pass"
        a_is_good = after.status == "pass"
        a_is_bad = after.status in ("crashed", "fail", "semantic_fail", "degraded_input")
        if b_was_bad and a_is_good:
            marker = "  [bold green]FIXED[/bold green]"
        elif b_was_good and a_is_bad:
            marker = "  [bold red]REGRESSION[/bold red]"
        else:
            marker = ""
        console.print(
            f"  [bold]{name}[/bold]{pad}  "
            f"[{b_style}]{b_label}[/{b_style}]"
            f"  [dim]→[/dim]  "
            f"[{a_style}]{a_label}[/{a_style}]"
            f"{marker}"
        )
    else:
        style = _STATUS_STYLE.get(after.status, "dim")
        label = _STATUS_LABEL.get(after.status, after.status)
        console.print(f"  [bold]{name}[/bold]{pad}  [{style}]{label}[/{style}]")

    dur_line = _format_duration_diff(before.duration_ms, after.duration_ms)
    if dur_line:
        console.print(f"       [dim]└─  {dur_line}[/dim]")

    for diff_line in field_diffs:
        console.print(f"       [dim]└─[/dim]  {diff_line}")

    for diff_line in inspection_diffs:
        console.print(f"       [dim]└─[/dim]  {diff_line}")

    for diff_line in validator_diffs:
        console.print(f"       [dim]└─[/dim]  {diff_line}")

    console.print()
    return True


def _print_frozen_node(
    name: str,
    event: NodeEvent,
    after: RunRecord,
    name_col: int,
) -> None:
    pad = " " * (name_col - len(name))
    style = _STATUS_STYLE.get(event.status, "dim")
    label = _STATUS_LABEL.get(event.status, event.status)
    note = "frozen · not re-run" if after.replay_from_step else "only in before"
    console.print(f"  [bold]{name}[/bold]{pad}  [{style}]{label}[/{style}]  [dim]{note}[/dim]")
    console.print()


def _print_new_node(name: str, event: NodeEvent, name_col: int) -> None:
    pad = " " * (name_col - len(name))
    style = _STATUS_STYLE.get(event.status, "dim")
    label = _STATUS_LABEL.get(event.status, event.status)
    console.print(
        f"  [bold]{name}[/bold]{pad}  [{style}]{label}[/{style}]  [dim]now reached[/dim]"
    )
    console.print()


# ── Output diff ───────────────────────────────────────────────────────────────


def _diff_output(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[str]:
    """Return rich-formatted diff lines for output_dict changes. Empty = no changes."""
    if before is None and after is None:
        return []
    if before is None:
        return ["[dim]no output before → output now present[/dim]"]
    if after is None:
        return ["[dim]had output → no output (crashed)[/dim]"]

    diffs: list[str] = []
    for key in sorted(set(before) | set(after)):
        escaped_key = escape(key)
        if key not in before:
            diffs.append(f'[dim]"[bold]{escaped_key}[/bold]" · data added[/dim]')
        elif key not in after:
            diffs.append(f'[dim]"[bold]{escaped_key}[/bold]" · data removed[/dim]')
        elif before[key] != after[key]:
            diffs.append(f'[dim]"[bold]{escaped_key}[/bold]" · data changed[/dim]')
    return diffs


# ── Inspection diff ──────────────────────────────────────────────────────────


def _diff_inspection(
    before: InspectionResult | None,
    after: InspectionResult | None,
) -> list[str]:
    """Diff argus inspection findings between two runs of the same node."""
    if before is None and after is None:
        return []

    diffs: list[str] = []

    b_missing = set(before.missing_fields) if before else set()
    a_missing = set(after.missing_fields) if after else set()
    b_empty = set(before.empty_fields) if before else set()
    a_empty = set(after.empty_fields) if after else set()
    b_severity = before.severity if before else "ok"
    a_severity = after.severity if after else "ok"

    # Fields that were missing but are now present
    resolved_missing = b_missing - a_missing
    for f in sorted(resolved_missing):
        diffs.append(
            "[bold green]✓[/bold green] "
            f'[dim]missing field "[bold]{escape(f)}[/bold]" resolved[/dim]'
        )

    # Fields that are now missing but weren't before
    new_missing = a_missing - b_missing
    for f in sorted(new_missing):
        diffs.append(
            f'[bold red]✗[/bold red] [dim]field "[bold]{escape(f)}[/bold]" now missing[/dim]'
        )

    # Fields that were empty but are now populated
    resolved_empty = b_empty - a_empty
    for f in sorted(resolved_empty):
        diffs.append(
            "[bold green]✓[/bold green] "
            f'[dim]empty field "[bold]{escape(f)}[/bold]" now populated[/dim]'
        )

    # Fields that are now empty but weren't before
    new_empty = a_empty - b_empty
    for f in sorted(new_empty):
        diffs.append(
            f'[bold yellow]~[/bold yellow] [dim]field "[bold]{escape(f)}[/bold]" now empty[/dim]'
        )

    # Tool failure changes
    b_tf = {(tf.field_name, tf.failure_type) for tf in (before.tool_failures if before else [])}
    a_tf = {(tf.field_name, tf.failure_type) for tf in (after.tool_failures if after else [])}

    for field_name, ftype in sorted(b_tf - a_tf):
        diffs.append(
            "[bold green]✓[/bold green] "
            f'[dim]tool {escape(ftype)} on "{escape(field_name)}" resolved[/dim]'
        )
    for field_name, ftype in sorted(a_tf - b_tf):
        diffs.append(
            f'[bold red]⚠[/bold red] [dim]new tool {escape(ftype)} on "{escape(field_name)}"[/dim]'
        )

    # Severity change
    _SEV_RANK = {"ok": 0, "info": 1, "warning": 2, "critical": 3}
    if b_severity != a_severity:
        b_rank = _SEV_RANK.get(b_severity, 0)
        a_rank = _SEV_RANK.get(a_severity, 0)
        if a_rank < b_rank:
            diffs.append(
                f"[bold green]✓[/bold green] [dim]severity {b_severity} → {a_severity}[/dim]"
            )
        else:
            diffs.append(
                f"[bold yellow]~[/bold yellow] [dim]severity {b_severity} → {a_severity}[/dim]"
            )

    return diffs


# ── Validator diff ───────────────────────────────────────────────────────────


def _diff_validators(
    before: list[ValidatorResult],
    after: list[ValidatorResult],
) -> list[str]:
    """Diff validator results between two runs of the same node."""
    b_map: dict[str, bool] = {v.validator_name: v.is_valid for v in before}
    a_map: dict[str, bool] = {v.validator_name: v.is_valid for v in after}

    all_names = sorted(set(b_map) | set(a_map))
    diffs: list[str] = []

    for vname in all_names:
        b_valid = b_map.get(vname)
        a_valid = a_map.get(vname)

        if b_valid is None and a_valid is not None:
            icon = "[bold green]✓[/bold green]" if a_valid else "[bold magenta]⊗[/bold magenta]"
            diffs.append(f"{icon} [dim]validator {escape(vname)} · new[/dim]")
        elif b_valid is not None and a_valid is None:
            diffs.append(f"[dim]validator {escape(vname)} · removed[/dim]")
        elif b_valid != a_valid:
            if not b_valid and a_valid:
                diffs.append(
                    "[bold green]✓[/bold green] "
                    f"[dim]validator {escape(vname)} · fail → pass[/dim]"
                )
            else:
                diffs.append(
                    "[bold magenta]⊗[/bold magenta] "
                    f"[dim]validator {escape(vname)} · pass → fail[/dim]"
                )

    return diffs


# ── Duration ──────────────────────────────────────────────────────────────────


def _format_duration_diff(before_ms: float | None, after_ms: float | None) -> str:
    """Format duration delta. Returns empty string when delta is trivial."""
    if before_ms is None or after_ms is None:
        return ""
    delta = after_ms - before_ms
    if abs(delta) < _DURATION_THRESHOLD_MS:
        return ""
    sign = "+" if delta >= 0 else ""
    if before_ms > 0:
        pct = (delta / before_ms) * 100
        return f"{before_ms:.0f} ms → {after_ms:.0f} ms  ({sign}{pct:.0f}%)"
    return f"{before_ms:.0f} ms → {after_ms:.0f} ms"


# ── Summary ───────────────────────────────────────────────────────────────────


def _print_summary(stats: dict[str, int]) -> None:
    parts: list[str] = []
    if stats["changed"]:
        n = stats["changed"]
        parts.append(f"[bold]{n}[/bold] node{'s' if n != 1 else ''} changed")
    if stats["fixed"]:
        parts.append(f"[bold green]{stats['fixed']}[/bold green] fixed")
    if stats["regressed"]:
        parts.append(f"[bold red]{stats['regressed']}[/bold red] regressed")
    if stats["new"]:
        parts.append(f"[bold]{stats['new']}[/bold] now reached")
    if stats["frozen"]:
        parts.append(f"[dim]{stats['frozen']} frozen[/dim]")
    if not parts:
        parts.append("[dim]no changes detected[/dim]")
    console.print("  " + "  ·  ".join(parts))


# ── Judge score deltas (PRD-09) ───────────────────────────────────────────────


def _scores_by_node(record: RunRecord) -> dict[tuple[str, str], list[JudgeScore]]:
    """Scores keyed by (validator, node): a wildcard judge scores every node, and a
    node in a loop scores once per pass, so name alone would collapse them."""
    grouped: dict[tuple[str, str], list[JudgeScore]] = {}
    # a retried / skipped pass does not count against the run, as in the findings
    inactive = {
        ev.step_index
        for ev in record.steps
        if ev.status in _INACTIVE and ev.step_index is not None
    }
    for js in record.judge_scores:
        if js.step_index in inactive:
            continue
        grouped.setdefault((js.validator_name, js.origin_node or "-"), []).append(js)
    return grouped


def _print_judge_score_deltas(before: RunRecord, after: RunRecord) -> None:
    """Print each judge's score change per node between two runs.

    Reads the persisted ``judge_scores`` only. The signed delta is
    direction-normalized, so positive is better for "maximize" and "minimize"
    alike; scores whose direction changed are not comparable and get no delta.
    Display only: never an exit code.
    """
    b_scores = _scores_by_node(before)
    a_scores = _scores_by_node(after)
    if not b_scores and not a_scores:
        return

    console.print()
    console.print(Text("  judge scores", style="bold italic"))
    for key in sorted(set(b_scores) | set(a_scores)):
        name, node = key
        passes = list(zip_longest(b_scores.get(key, []), a_scores.get(key, [])))
        for i, (b, a) in enumerate(passes):
            pass_no = f" #{i + 1}" if len(passes) > 1 else ""
            label = f"{escape(name)} [dim]@ {escape(node)}{pass_no}[/dim]"
            if a is None or b is None:
                side = "new" if b is None else "removed"
                icon = "[bold green]✓[/bold green]" if b is None else "[dim]−[/dim]"
                console.print(f"  {icon}  {label}  [dim]{side}[/dim]")
            elif a.direction != b.direction:
                console.print(
                    f"  [dim]■[/dim]  {label}  {b.score:.2f} → {a.score:.2f}  "
                    f"[dim]direction changed ({b.direction} → {a.direction}), not compared[/dim]"
                )
            else:
                # Positive is better: for "minimize" a lower score is the gain.
                delta = (a.score - b.score) if a.direction == "maximize" else (b.score - a.score)
                marker = (
                    "[bold green]▲[/bold green]"
                    if delta > 0
                    else "[bold red]▼[/bold red]"
                    if delta < 0
                    else "[dim]■[/dim]"
                )
                console.print(
                    f"  {marker}  {label}  {b.score:.2f} → {a.score:.2f}  "
                    f"Δ {delta:+.2f}  [dim]({a.direction})[/dim]"
                )
