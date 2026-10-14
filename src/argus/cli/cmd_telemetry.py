"""argus telemetry — see or change what Argus sends (docs/DATA.md)."""

from __future__ import annotations

from rich.console import Console

from argus.telemetry import send

_console = Console()


def telemetry_set(enabled: bool) -> None:
    send.set_enabled(enabled)
    on, source = send.status()
    word = "[green]on[/green]" if on else "[yellow]off[/yellow]"
    _console.print(f"  Telemetry is {word} (saved to {send.SETTINGS_FILE}).")
    if on != enabled:
        _console.print(f"  [dim]{source} overrides the saved setting.[/dim]")


def telemetry_status() -> None:
    on, source = send.status()
    word = "[green]on[/green]" if on else "[yellow]off[/yellow]"
    _console.print(f"  Telemetry is {word} ({source}).")
    if send.ENDPOINT is None:
        _console.print("  [dim]No intake configured in this build: nothing is sent.[/dim]")
    _console.print(f"  [dim]Sent cards are kept in {send.outbox_dir()}[/dim]")


def telemetry_show_last() -> None:
    path = send.last_card()
    if path is None:
        _console.print("  No card sent from this project yet.")
        return
    _console.print(f"  [dim]{path}[/dim]")
    print(path.read_text(encoding="utf-8"))  # noqa: T201  raw JSON, pipeable
