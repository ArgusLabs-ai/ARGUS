"""Cross-run finding hotspots for the local dashboard (PRD US-4.4)."""

from __future__ import annotations

from typing import Any

HOTSPOT_RUN_CAP = 500


def _finding_ends(finding: dict[str, Any]) -> tuple[str, str] | None:
    node = finding.get("node")
    if not node:
        return None
    origin = finding.get("origin_node") or node
    return str(origin), str(node)


def finding_index(run: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Unique origin nodes and finding nodes on one run payload."""
    origins: list[str] = []
    nodes: list[str] = []
    seen_o: set[str] = set()
    seen_n: set[str] = set()
    for finding in run.get("findings") or []:
        ends = _finding_ends(finding)
        if ends is None:
            continue
        origin, node = ends
        if origin not in seen_o:
            seen_o.add(origin)
            origins.append(origin)
        if node not in seen_n:
            seen_n.add(node)
            nodes.append(node)
    return origins, nodes


def _matches_tag(run: dict[str, Any], tag: str) -> bool:
    key, sep, value = tag.partition(":")
    if not sep:
        tags = run.get("tags") or {}
        return tag in tags or tag in str(tags)
    if key == "status":
        return run.get("overall_status") == value
    if key == "node":
        return run.get("first_failure_step") == value or value in (
            run.get("graph_node_names") or []
        )
    if key == "origin":
        return any(
            (_finding_ends(f) or (None, None))[0] == value
            for f in (run.get("findings") or [])
        )
    tags = run.get("tags") or {}
    if isinstance(tags, dict):
        return tags.get(key) == value or value in tags.get(key, [])
    return False


def aggregate_hotspots(
    runs: list[dict[str, Any]],
    *,
    tag: str | None = None,
    cap: int = HOTSPOT_RUN_CAP,
) -> dict[str, Any]:
    """Count unsuppressed findings as origin_node × node across newest ``cap`` runs."""
    selected = runs[:cap]
    if tag:
        selected = [run for run in selected if _matches_tag(run, tag)]

    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for run in selected:
        rid = run.get("run_id")
        for finding in run.get("findings") or []:
            if finding.get("suppressed"):
                continue
            ends = _finding_ends(finding)
            if ends is None:
                continue
            origin, node = ends
            slot = cells.setdefault(
                (origin, node),
                {"origin": origin, "node": node, "count": 0, "run_ids": []},
            )
            slot["count"] += 1
            if rid and rid not in slot["run_ids"]:
                slot["run_ids"].append(rid)

    return {
        "origins": sorted({origin for origin, _ in cells}),
        "nodes": sorted({node for _, node in cells}),
        "cells": [cells[key] for key in sorted(cells)],
        "run_count": len(selected),
    }
