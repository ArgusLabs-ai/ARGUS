"""``argus ingest`` — grade an exported trace file with no live app."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from argus.grading import IncompleteTraceError
from argus.ingest.langsmith import CloudSyncRefused, ingest_langsmith, load_edges

console = Console()


def ingest_langsmith_file(path: Path, *, allow_cloud: bool, edges: Path | None = None) -> None:
    """Grade ``path`` and save the run; exit 2 when it cannot be graded.

    The session prints the one-line verdict as it saves, as it does for a live run.
    """
    try:
        graph = load_edges(edges) if edges is not None else None
        ingest_langsmith(path, allow_cloud=allow_cloud, edges=graph)
    except (CloudSyncRefused, IncompleteTraceError, ValueError, OSError) as e:
        # A bad JSON line is a ValueError too.
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2) from e
    except KeyError as e:
        console.print(f"[red]Error:[/red] {path} is not a LangSmith run export: {e}")
        raise typer.Exit(2) from e
