"""`argus ui` JSON endpoints the dashboard actually calls."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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


# ── rerun: a trace's code comes from the app factory, never a guess (#79) ────

_APP = '''
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class S(TypedDict, total=False):
    query: str
    summary: str


def build():
    g = StateGraph(S)
    g.add_node("summarize", lambda s: {"summary": "FIXED: " + s["query"]})
    g.add_edge(START, "summarize")
    g.add_edge("summarize", END)
    return g  # the builder, not .compile(): both shapes must rerun


def forgot_return():
    build()
'''


@pytest.fixture
def ui(tmp_path: Path) -> Iterator[Callable[[Optional[str]], str]]:
    """`argus ui`'s handler over tmp_path, started with a given app factory."""
    servers: list[ThreadingHTTPServer] = []

    def start(app: Optional[str]) -> str:
        argus_dir = tmp_path / ".argus"
        handler = _make_handler(argus_dir / "runs", argus_dir / "logs", app, tmp_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _trace_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    pytest.importorskip("langgraph")
    from argus.recorder import ArgusRecorder

    (tmp_path / "replay_app.py").write_text(_APP)
    monkeypatch.syspath_prepend(str(tmp_path))
    import replay_app  # noqa: PLC0415

    rec = ArgusRecorder(semantic_judge=False)
    rec.attach(replay_app.build().compile()).invoke({"query": "refunds"})
    return rec.session.run_id


def _rerun_node(base: str, run_id: str) -> tuple[int, dict[str, Any]]:
    """POST a single-node rerun; a 202 is followed until the job settles."""
    body = json.dumps({"run_id": run_id, "from_step": "summarize", "mode": "node"}).encode()
    req = Request(f"{base}/api/replay", data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req) as resp:  # noqa: S310 - localhost test server
            status, payload = resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())
    deadline = time.time() + 10
    job: dict[str, Any] = {"status": "running"}
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
        job = _get(base, f"/api/replay/status/{payload['job_id']}")
    return status, job


def test_a_node_rerun_of_a_trace_takes_the_node_from_the_app_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ui: Callable[[Optional[str]], str]
) -> None:
    from argus.storage import load_run

    run_id = _trace_run(tmp_path, monkeypatch)
    status, job = _rerun_node(ui("replay_app:build"), run_id)
    assert status == 202
    assert job["status"] == "done", job

    rerun = load_run(job["run_id"])
    assert rerun.parent_run_id == run_id
    assert rerun.steps[0].output_dict == {"summary": "FIXED: refunds"}
    # Nothing was guessed and written back into the trace.
    assert not load_run(run_id).node_fn_refs


def test_a_node_rerun_of_a_trace_with_no_factory_asks_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ui: Callable[[Optional[str]], str]
) -> None:
    monkeypatch.setattr("argus.cli.cmd_open_ui._load_config_app_factory", lambda: None)
    run_id = _trace_run(tmp_path, monkeypatch)
    status, body = _rerun_node(ui(None), run_id)
    assert status == 422
    assert body["error"] == "no_app_factory"


@pytest.mark.parametrize("factory", ["replay_app:forgot_return", "no_such_module:build"])
def test_a_factory_that_builds_no_graph_is_asked_for_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ui: Callable[[Optional[str]], str],
    factory: str,
) -> None:
    """`bad_factory` re-opens the prompt; anything else left a broken saved spec retried."""
    run_id = _trace_run(tmp_path, monkeypatch)
    status, job = _rerun_node(ui(factory), run_id)
    assert status == 202
    assert job["status"] == "error"
    assert job["error_code"] == "bad_factory", job
