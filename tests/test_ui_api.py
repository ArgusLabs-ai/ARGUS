"""`argus ui` JSON endpoints the dashboard actually calls."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest

from argus.cli.cmd_open_ui import _make_handler

pytestmark = pytest.mark.unit


def _run_payload(run_id: str, *, findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "overall_status": "silent_failure",
        "started_at": f"2026-01-0{run_id[-1]}T00:00:00",
        "duration_ms": 12.0,
        "steps": [{"node_name": "search"}, {"node_name": "answer"}],
        "first_failure_step": "answer",
        "graph_node_names": ["__start__", "search", "answer", "__end__"],
        "argus_version": "0.1.0",
        "findings": findings,
    }


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    runs = tmp_path / ".argus" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for rid, findings in (
        ("run-1", [{"node": "answer", "origin_node": "search"}]),
        ("run-2", [{"node": "answer", "origin_node": "search", "suppressed": True}]),
    ):
        (runs / f"{rid}.json").write_text(json.dumps(_run_payload(rid, findings=findings)))

    handler = _make_handler(runs, tmp_path / ".argus" / "logs", None, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(base: str, path: str) -> Any:
    with urlopen(f"{base}{path}") as resp:  # noqa: S310 - localhost test server
        return json.loads(resp.read())


def test_run_summaries_carry_origin_and_finding_nodes(server: str) -> None:
    """The origin / node filter chips read these; without them they collapse."""
    summaries = {r["run_id"]: r for r in _get(server, "/api/runs")}
    assert summaries["run-1"]["origins"] == ["search"]
    assert summaries["run-1"]["finding_nodes"] == ["answer"]


def test_hotspots_endpoint_aggregates_across_runs(server: str) -> None:
    data = _get(server, "/api/hotspots")
    assert data["run_count"] == 2
    # run-2's only finding is suppressed, so one cell from one run.
    assert data["cells"] == [
        {"origin": "search", "node": "answer", "count": 1, "run_ids": ["run-1"]}
    ]


def test_hotspots_endpoint_honours_a_tag(server: str) -> None:
    assert _get(server, "/api/hotspots?tag=status:clean")["run_count"] == 0


def test_fix_endpoint_rejects_an_empty_run_id(server: str) -> None:
    """`startswith("")` matches every file, so an empty id must not pick one."""
    from urllib.error import HTTPError

    with pytest.raises(HTTPError) as exc:
        _get(server, "/api/runs//fix")
    assert exc.value.code == 404
