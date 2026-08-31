"""``argus check`` — CI gate: exit 1 when a recorded run was not clean."""

from __future__ import annotations

import os

import typer
from rich.console import Console
from rich.text import Text

from argus.check import evaluate_run
from argus.cli import print_footer
from argus.findings import format_run_finding
from argus.storage import last_run_id, load_run, resolve_run_path

console = Console()

_STATUS_STYLE = {
    "clean": "bold green",
    "silent_failure": "bold yellow",
    "crashed": "bold red",
    "semantic_fail": "bold magenta",
    "interrupted": "bold yellow",
}


def check_run(run_id: str | None) -> None:
    """Load an explicit, environment-selected, or recent run and grade it."""
    target = run_id if run_id not in (None, "last", "run") else None
    if target is None:
        target = os.environ.get("ARGUS_RUN_ID", "").strip() or last_run_id()
        if target is None:
            console.print("[red]Error:[/red] No runs found in .argus/runs/.")
            raise typer.Exit(1)

    try:
        path = resolve_run_path(target)
        record = load_run(target)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e

    result = evaluate_run(record)
    style = _STATUS_STYLE.get(record.overall_status, "dim")

    console.print()
    header = Text()
    header.append("argus check", style="bold italic")
    header.append(f"  {record.run_id}", style="italic dim")
    console.print(f"  {header}")
    console.print(f"  [dim]checked[/dim]  {path}", soft_wrap=True)

    status_line = Text()
    status_line.append("  status  ", style="dim")
    status_line.append(record.overall_status, style=style)
    console.print(status_line)

    if result.passed:
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
