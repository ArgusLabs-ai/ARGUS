"""``argus ingest`` — grade an exported trace file with no live app."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from argus.grading import IncompleteTraceError
from argus.ingest.langsmith import CloudSyncRefused, ingest_langsmith, load_edges

console = Console()


def load_consumers(path: Path) -> dict[str, list[str]]:
    """Read a ``{"field": ["reader", ...]}`` file; ``ValueError`` when it is not one."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (
        isinstance(data, dict)
        and all(
            isinstance(readers, list) and all(isinstance(r, str) for r in readers)
            for readers in data.values()
        )
    ):
        raise ValueError(f'{path} is not a consumers file: expected {{"field": ["reader", ...]}}')
    return data


def ingest_langsmith_file(
    path: Path,
    *,
    allow_cloud: bool,
    edges: Path | None = None,
    consumers: Path | None = None,
) -> None:
    """Grade ``path`` and save the run; exit 2 when it cannot be graded.

    The session prints the one-line verdict as it saves, as it does for a live run.
    """
    try:
        graph = load_edges(edges) if edges is not None else None
        readers = load_consumers(consumers) if consumers is not None else None
        ingest_langsmith(path, allow_cloud=allow_cloud, edges=graph, consumers=readers)
    except (CloudSyncRefused, IncompleteTraceError, ValueError, OSError) as e:
        # A bad JSON line is a ValueError too.
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2) from e
    except KeyError as e:
        console.print(f"[red]Error:[/red] {path} is not a LangSmith run export: {e}")
        raise typer.Exit(2) from e
