from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
import urllib.request

from rich.console import Console

_console = Console()

_REPO = "ArgusLabs-ai/ARGUS"
_PACKAGE = "argus-agents"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"


def _current_version() -> str:
    try:
        return importlib.metadata.version(_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _latest_release() -> str | None:
    try:
        req = urllib.request.Request(
            _API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "argus-cli"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        tag: str = data["tag_name"]
        return tag.lstrip("v")
    except Exception:
        return None


def _parse(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except ValueError:
        return (0,)


def check_for_update() -> None:
    current = _current_version()
    _console.print(f"\n  [dim]current version[/dim]  [bold]{current}[/bold]")
    _console.print("  [dim]checking GitHub for updates…[/dim]")

    latest = _latest_release()
    if latest is None:
        _console.print("  [yellow]Could not reach GitHub — check your connection.[/yellow]\n")
        return

    if _parse(latest) <= _parse(current):
        _console.print(
            f"  [green]✓[/green]  [dim]already up to date[/dim]  [bold]({current})[/bold]\n"
        )
        return

    _console.print(
        f"  [bold yellow]↑[/bold yellow]  new release available: "
        f"[bold]{latest}[/bold]  (you have {current})"
    )
    _console.print(f"  [dim]https://github.com/{_REPO}/releases/tag/v{latest}[/dim]")
    _console.print()
    _console.print("  [dim]upgrading via pip…[/dim]")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", _PACKAGE],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        _console.print(f"  [green]✓[/green]  updated to [bold]{latest}[/bold]\n")
    else:
        _console.print(f"  [red]pip upgrade failed[/red]\n{result.stderr}\n")
