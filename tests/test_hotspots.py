"""US-4.4: origin × node hotspot aggregation for the dashboard."""

from __future__ import annotations

import pytest

from argus.hotspots import aggregate_hotspots, finding_index

pytestmark = pytest.mark.unit


def _run(run_id: str, findings: list[dict], status: str = "silent_failure") -> dict:
    return {
        "run_id": run_id,
        "overall_status": status,
        "findings": findings,
        "graph_node_names": ["search", "answer"],
        "first_failure_step": "search",
    }


def test_aggregate_counts_origin_by_node() -> None:
    runs = [
        _run(
            "a",
            [
                {"node": "answer", "origin_node": "search", "suppressed": False},
                {"node": "answer", "origin_node": "search", "suppressed": False},
            ],
        ),
        _run("b", [{"node": "rank", "origin_node": "search", "suppressed": False}]),
    ]
    result = aggregate_hotspots(runs)
    assert result["run_count"] == 2
    assert result["origins"] == ["search"]
    assert result["nodes"] == ["answer", "rank"]
    by_pair = {(c["origin"], c["node"]): c for c in result["cells"]}
    assert by_pair[("search", "answer")]["count"] == 2
    assert by_pair[("search", "answer")]["run_ids"] == ["a"]
    assert by_pair[("search", "rank")]["count"] == 1


def test_aggregate_skips_suppressed_and_missing_node() -> None:
    runs = [
        _run(
            "a",
            [
                {"node": "answer", "origin_node": "search", "suppressed": True},
                {"origin_node": "search"},
            ],
        )
    ]
    assert aggregate_hotspots(runs)["cells"] == []


def test_aggregate_uses_node_when_origin_missing() -> None:
    result = aggregate_hotspots([_run("a", [{"node": "inc"}])])
    assert result["cells"] == [
        {"origin": "inc", "node": "inc", "count": 1, "run_ids": ["a"]}
    ]


def test_aggregate_filters_status_tag() -> None:
    runs = [
        _run("dirty", [{"node": "answer", "origin_node": "search"}]),
        _run("ok", [], status="clean"),
    ]
    result = aggregate_hotspots(runs, tag="status:clean")
    assert result["run_count"] == 1
    assert result["cells"] == []


def test_finding_index_dedupes() -> None:
    origins, nodes = finding_index(
        _run(
            "a",
            [
                {"node": "answer", "origin_node": "search"},
                {"node": "answer", "origin_node": "search"},
            ],
        )
    )
    assert origins == ["search"]
    assert nodes == ["answer"]
