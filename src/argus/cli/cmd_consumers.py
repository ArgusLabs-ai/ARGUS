"""``argus consumers`` — propose a consumer map from a recorded healthy run."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from argus.contextual import propose_consumers
from argus.replay import _rows
from argus.storage import load_run

console = Console()


def propose_for_run(run_id: str, write: Path | None = None) -> dict[str, list[str]]:
    """Load ``run_id`` and return the candidate map. Optionally write it.

    Writing the file does not change grading. The user edits it and passes it
    to ``ArgusRecorder(consumers=...)``.
    """
    try:
        record = load_run(run_id)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    proposed = propose_consumers(_rows(record))
    text = json.dumps(proposed, indent=2) + "\n"
    if write is not None:
        write.write_text(text, encoding="utf-8")
        console.print(
            f"wrote {write}. Edit the readers, then pass the file to "
            "ArgusRecorder. ARGUS does not load it on its own."
        )
    else:
        console.print(text, end="")
        console.print(
            "[dim]Not applied. Pass --write and edit the file before using it as consumers=.[/dim]"
        )
    return proposed
