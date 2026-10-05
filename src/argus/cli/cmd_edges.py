"""``argus edges`` — write a graph's topology for ``argus ingest --edges``."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from argus.cli.cmd_replay import _import_factory

console = Console()


def export_edges(spec: str, out: Path) -> None:
    """Compile the graph from ``module:factory`` once and write its topology.

    The topology is read the way the recorder reads it at attach, so an
    ingested run gets the same edges a live run would. Exit 2 when the factory
    cannot be imported or does not return a compiled graph.
    """
    factory = _import_factory(spec)
    if factory is None:
        raise typer.Exit(2)
    graph = factory()
    if not hasattr(graph, "get_graph"):
        console.print(f"[red]Error:[/red] '{spec}' must return a compiled graph (call .compile())")
        raise typer.Exit(2)

    from argus.recorder import _topology

    names, edge_map, conditional_sources, subgraph_parents = _topology(graph)
    payload = {
        "edge_map": edge_map,
        "conditional_sources": sorted(conditional_sources),
        "node_names": names,
        "subgraph_parents": sorted(subgraph_parents),
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    console.print(f"wrote {out}: {len(names)} nodes, {len(conditional_sources)} conditional")
