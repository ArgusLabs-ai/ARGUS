"""``argus check`` — CI gate: exit 1 when a recorded run was not clean."""

from __future__ import annotations

import dataclasses
import json
import sys
from typing import Any

import typer
from rich.console import Console
from rich.text import Text

from argus.check import evaluate_run
from argus.cli import print_footer
from argus.findings import format_run_finding
from argus.models import RunRecord
from argus.storage import last_run_id, load_run

console = Console()

# Run statuses a user may pass to --fail-on. "clean" is never a failure; the
# node → run roll-up (docs/STATUS.md) means these are the only values that occur.
FAIL_ON_CHOICES = ("crashed", "interrupted", "silent_failure")

_STATUS_STYLE = {
    "clean": "bold green",
    "silent_failure": "bold yellow",
    "crashed": "bold red",
    "semantic_fail": "bold magenta",
    "interrupted": "bold yellow",
}


def parse_fail_on(raw: str | None) -> frozenset[str] | None:
    """Parse ``--fail-on a,b``. ``None`` means the default gate (any non-clean)."""
    if raw is None or not raw.strip():
        return None
    values = frozenset(v.strip() for v in raw.split(",") if v.strip())
    bad = sorted(values - set(FAIL_ON_CHOICES))
    if bad:
        choices = ", ".join(FAIL_ON_CHOICES)
        raise ValueError(f"unknown --fail-on value(s): {', '.join(bad)}; choose from {choices}")
    return values


def _passed(record: RunRecord, result_passed: bool, fail_on: frozenset[str] | None) -> bool:
    if fail_on is None:
        return result_passed
    return record.overall_status not in fail_on


def check_payload(record: RunRecord, fail_on: frozenset[str] | None = None) -> dict[str, Any]:
    """Machine-readable verdict for ``--format json``. Stable keys; additive only."""
    result = evaluate_run(record)
    return {
        "run_id": record.run_id,
        "schema_version": record.schema_version,
        "overall_status": record.overall_status,
        "passed": _passed(record, result.passed, fail_on),
        "fail_on": sorted(fail_on) if fail_on is not None else None,
        "first_failure_step": record.first_failure_step,
        "root_cause_chain": list(record.root_cause_chain),
        "failing_nodes": list(result.failing_nodes),
        "reasons": list(result.reasons),
        "findings": [dataclasses.asdict(f) for f in record.findings],
    }


def _emit_error(message: str, *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps({"error": message}) + "\n")
    else:
        console.print(f"[red]Error:[/red] {message}")


def check_run(
    run_id: str | None,
    *,
    output_format: str = "text",
    fail_on: str | None = None,
) -> None:
    """Load ``run_id`` (or the most recent run) and exit 1 if it is not clean.

    ``output_format="json"`` prints one JSON object to stdout and nothing else;
    exit codes are unchanged (0 pass, 1 fail, 2 usage error).
    """
    as_json = output_format == "json"
    if output_format not in ("text", "json"):
        _emit_error(f"unknown --format {output_format!r}; use text or json", as_json=False)
        raise typer.Exit(2)
    try:
        fail_on_set = parse_fail_on(fail_on)
    except ValueError as e:
        _emit_error(str(e), as_json=as_json)
        raise typer.Exit(2) from e

    target = run_id
    if target is None or target in ("last", "run"):
        target = last_run_id()
        if target is None:
            _emit_error("No runs found in .argus/runs/.", as_json=as_json)
            raise typer.Exit(1)

    try:
        record = load_run(target)
    except (FileNotFoundError, ValueError) as e:
        _emit_error(str(e), as_json=as_json)
        raise typer.Exit(1) from e

    if as_json:
        payload = check_payload(record, fail_on_set)
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
        raise typer.Exit(0 if payload["passed"] else 1)

    result = evaluate_run(record)
    passed = _passed(record, result.passed, fail_on_set)
    style = _STATUS_STYLE.get(record.overall_status, "dim")

    console.print()
    header = Text()
    header.append("argus check", style="bold italic")
    header.append(f"  {record.run_id}", style="italic dim")
    console.print(f"  {header}")

    status_line = Text()
    status_line.append("  status  ", style="dim")
    status_line.append(record.overall_status, style=style)
    console.print(status_line)

    if fail_on_set is not None:
        console.print(f"  [dim]fail-on {', '.join(sorted(fail_on_set))}[/dim]")

    if passed:
        console.print("  [bold green]✓[/bold green]  pass")
        console.print()
        print_footer()
        raise typer.Exit(0)

    finding = format_run_finding(record)
    if finding:
        console.print()
        console.print(finding)
    elif result.reasons:
        console.print()
        for reason in result.reasons:
            console.print(f"  [dim]└─[/dim]  {reason}")

    console.print()
    console.print("  [bold red]✗[/bold red]  fail  —  pipeline was not clean")
    console.print()
    print_footer()
    raise typer.Exit(1)
