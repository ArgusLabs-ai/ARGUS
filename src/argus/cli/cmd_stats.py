"""CLI command: argus stats — signature effectiveness reporting."""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

_console = Console()


def stats(
    all_sigs: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include builtin signatures"),
    ] = False,
    sig: Annotated[
        Optional[str],
        typer.Option("--sig", "-s", help="Show stats for a specific signature ID"),
    ] = None,
    disable: Annotated[
        Optional[str],
        typer.Option("--disable", help="Disable a custom signature by ID"),
    ] = None,
    enable: Annotated[
        Optional[str],
        typer.Option("--enable", help="Re-enable a disabled custom signature"),
    ] = None,
    dispute: Annotated[
        Optional[str],
        typer.Option("--dispute", help="Flag a signature hit as false positive (sig_id)"),
    ] = None,
    run_id: Annotated[
        Optional[str],
        typer.Option("--run", help="Run ID for dispute context"),
    ] = None,
    prune: Annotated[
        bool,
        typer.Option("--prune", help="Remove stale signatures that haven't proven useful"),
    ] = False,
    judge_run: Optional[str] = None,
) -> None:
    """Show signature effectiveness stats and manage learned patterns.

    ``judge_run`` (the ``<run>`` argument) shows that run's per-validator judge
    scores instead (PRD-09).
    """

    if judge_run is not None:
        _print_judge_scores(judge_run)
        return

    # Handle prune
    if prune:
        from argus.signature_stats import prune_stale_signatures

        removed = prune_stale_signatures()
        if removed:
            for rid in removed:
                _console.print(f"[yellow]Pruned[/yellow] {rid}")
            _console.print(f"\n[bold]{len(removed)}[/bold] stale signature(s) removed.")
        else:
            _console.print("[dim]No stale signatures to prune.[/dim]")
        return

    # Handle disable
    if disable is not None:
        from argus.candidate_store import disable_custom_signature

        if disable_custom_signature(disable):
            _console.print(f"[yellow]Disabled[/yellow] signature {disable}")
        else:
            _console.print(f"[red]Signature {disable} not found[/red]")
        return

    # Handle enable
    if enable is not None:
        from argus.candidate_store import enable_custom_signature

        if enable_custom_signature(enable):
            _console.print(f"[green]Enabled[/green] signature {enable}")
        else:
            _console.print(f"[red]Signature {enable} not found[/red]")
        return

    # Handle dispute
    if dispute is not None:
        from argus.signature_stats import record_dispute

        dispute_id = record_dispute(
            sig_id=dispute,
            run_id=run_id or "",
            node_name="",
            field_path="",
            evidence="",
            reason="flagged via CLI",
        )
        _console.print(f"[yellow]Recorded dispute[/yellow] {dispute_id} for {dispute}")
        return

    # Compute and display stats
    from argus.signature_stats import compute_stats

    sig_ids = [sig] if sig else None
    all_stats = compute_stats(sig_ids=sig_ids, include_builtins=all_sigs)

    if not all_stats:
        _console.print("[dim]No signature hits found in stored runs.[/dim]")
        return

    # Single signature detail view
    if sig and sig in all_stats:
        s = all_stats[sig]
        _console.print()
        _console.print(f"[bold]{s.sig_id}[/bold] ({s.source})")
        _console.print(f"  Description: {s.description}")
        _console.print(f"  Total hits:  {s.total_hits}")
        _console.print(f"  Runs hit:    {s.runs_hit}")
        _console.print(f"  Nodes hit:   {s.nodes_hit}")
        _console.print(f"  First hit:   {s.first_hit or 'never'}")
        _console.print(f"  Last hit:    {s.last_hit or 'never'}")
        _console.print(f"  False pos:   {s.false_positive_count}")
        _console.print(f"  Disabled:    {s.disabled}")
        if s.hit_nodes:
            _console.print(f"  Nodes:       {', '.join(s.hit_nodes)}")
        _console.print()
        return

    # Table view
    table = Table(title="Signature Effectiveness Report", show_lines=False)
    table.add_column("ID", style="bold")
    table.add_column("Source")
    table.add_column("Hits", justify="right")
    table.add_column("Runs", justify="right")
    table.add_column("FP", justify="right")
    table.add_column("Last Hit")
    table.add_column("Status")
    table.add_column("Description", max_width=40)

    for s in sorted(all_stats.values(), key=lambda x: x.total_hits, reverse=True):
        source_style = {
            "learned": "cyan",
            "shared": "magenta",
            "builtin": "dim",
        }.get(s.source, "")

        status = "[red]disabled[/red]" if s.disabled else "[green]active[/green]"
        last_hit = s.last_hit[:10] if s.last_hit else "never"
        fp_str = str(s.false_positive_count) if s.false_positive_count else "0"

        table.add_row(
            s.sig_id,
            f"[{source_style}]{s.source}[/{source_style}]",
            str(s.total_hits),
            str(s.runs_hit),
            fp_str,
            last_hit,
            status,
            s.description[:40] if s.description else "",
        )

    _console.print()
    _console.print(table)
    _console.print()


def _print_judge_scores(run_id: str) -> None:
    """One persisted run's judge scores. A pure read: nothing is recomputed.

    Everything in a row came from user code or a run file, so it is escaped
    before Rich reads it as markup.
    """
    from argus.findings import _INACTIVE
    from argus.storage import load_run

    try:
        record = load_run(run_id)
    except (FileNotFoundError, ValueError) as e:
        _console.print(f"[red]Error:[/red] {escape(str(e))}")
        raise typer.Exit(1) from e

    # a retried / skipped pass does not count against the run, as in the findings
    inactive = {
        ev.step_index
        for ev in record.steps
        if ev.status in _INACTIVE and ev.step_index is not None
    }
    scores = [js for js in record.judge_scores if js.step_index not in inactive]
    if not scores:
        _console.print(f"[dim]No judge scores recorded for run {escape(record.run_id)}.[/dim]")
        return

    table = Table(title=f"Judge scores  {escape(record.run_id)}", show_lines=False)
    table.add_column("Validator", style="bold")
    table.add_column("Node")
    table.add_column("Step", justify="right")
    table.add_column("Direction")
    table.add_column("Score", justify="right")
    table.add_column("Label")
    table.add_column("Explanation", max_width=50)

    for js in scores:
        style = "yellow" if js.severity == "warning" else "green"
        table.add_row(
            escape(js.validator_name),
            escape(js.origin_node or "-"),
            "-" if js.step_index is None else str(js.step_index),
            js.direction,
            f"[{style}]{js.score:.2f}[/{style}]",
            escape(js.label),
            escape(js.explanation),
        )

    _console.print()
    _console.print(table)
    _console.print()
