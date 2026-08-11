"""argus key — manage the local BYOK OpenAI API key."""

from __future__ import annotations

from rich.console import Console

from argus import user_config

_console = Console()


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}…{key[-4:]}"


def key_set(value: str | None = None) -> None:
    """Save an OpenAI key locally (prompts if not passed)."""
    if not value:
        import getpass

        value = getpass.getpass("OpenAI API key (input hidden): ").strip()
    if not value:
        _console.print("  [red]No key entered.[/red]")
        return
    user_config.set_openai_key(value)
    _console.print(f"  [green]Saved[/green] key {_mask(value)} to ~/.argus/config.json")
    _console.print(
        "  [dim]BYOK enabled — AI-powered detection now uses your key.\n"
        "  Next: wrap your pipeline with [bold]ArgusWatcher[/bold], run it, then "
        "[bold]argus show[/bold] / [bold]argus ui[/bold].\n"
        "  Check status anytime with [bold]argus doctor[/bold].[/dim]"
    )


def key_show() -> None:
    """Show the currently resolved key (masked) and its source."""
    import os

    env = os.environ.get("OPENAI_API_KEY")
    saved = user_config.get_saved_openai_key()
    if env:
        _console.print(f"  key: {_mask(env)}  [dim](source: OPENAI_API_KEY env)[/dim]")
    elif saved:
        _console.print(f"  key: {_mask(saved)}  [dim](source: ~/.argus/config.json)[/dim]")
    else:
        _console.print(
            "  [yellow]No OpenAI key set.[/yellow] Run [bold]argus key set[/bold] "
            "or export OPENAI_API_KEY."
        )


def key_clear() -> None:
    """Remove the saved local key."""
    user_config.clear_openai_key()
    _console.print("  [green]Cleared[/green] saved key from ~/.argus/config.json")
