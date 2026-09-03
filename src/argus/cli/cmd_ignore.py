"""``argus ignore`` — silence a signature project-wide or on one node."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from argus import suppressions as sup

_console = Console()


def ignore_list() -> None:
    items = sup.load_suppressions()
    if not items:
        _console.print(
            "  [dim]no suppressions[/dim]  ·  add one with  argus ignore <SIG-ID> [--node <name>]"
        )
        return
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("signature")
    table.add_column("node")
    for s in items:
        table.add_row(s.id, s.node or "[dim]every node[/dim]")
    _console.print(table)
    _console.print(f"  [dim]{sup.config_path()}[/dim]")


def ignore_add(sig_id: str, node: str | None) -> bool:
    try:
        added = sup.add_suppression(sig_id, node)
    except ValueError as e:
        _console.print(f"  [red]{e}[/red]")
        return False
    s = sup.Suppression(id=sig_id.strip().upper(), node=node or None)
    if added:
        _console.print(f"  [yellow]ignoring[/yellow] {s.label}")
    else:
        _console.print(f"  [dim]already ignoring[/dim] {s.label}")
    _console.print("  [dim]suppressed hits stay visible in `argus stats` and run findings[/dim]")
    return True


def ignore_remove(sig_id: str, node: str | None) -> bool:
    s = sup.Suppression(id=sig_id.strip().upper(), node=node or None)
    if sup.remove_suppression(sig_id, node):
        _console.print(f"  [green]restored[/green] {s.label}")
        return True
    _console.print(f"  [red]not found:[/red] {s.label}  ·  see  argus ignore --list")
    return False
