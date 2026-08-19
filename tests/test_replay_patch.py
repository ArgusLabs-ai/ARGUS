"""End-to-end tests for patched replay (time-travel part 2).

Verifies that a patch applied to a recorded node's input state actually
reaches the resumed trajectory, that upstream nodes stay frozen, and that
the patch is persisted on the replay run for provenance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from argus.models import LLMInvestigationConfig
from argus.replay import ReplayEngine
from argus.session import ArgusSession
from argus.state_patch import PatchError
from argus.storage import load_run

_MODULE_NAME = "argus_patch_pipeline"

_PIPELINE_SRC = '''
"""Throwaway pipeline used by the patched-replay tests."""

CALLS = []


def fetch(state):
    CALLS.append("fetch")
    return {"docs": ["d1", "d2"], "status": "PLACEHOLDER"}


def transform(state):
    CALLS.append("transform")
    return {"summary": "docs=%d status=%s" % (len(state["docs"]), state["status"])}


def finish(state):
    CALLS.append("finish")
    return {"done": True, "final": state["summary"]}
'''

_ORDER = ["fetch", "transform", "finish"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Clean tmp cwd, importable pipeline module, no LLM calls."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / f"{_MODULE_NAME}.py").write_text(_PIPELINE_SRC, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)
    yield
    sys.modules.pop(_MODULE_NAME, None)


def _pipeline():
    import importlib

    sys.modules.pop(_MODULE_NAME, None)
    return importlib.import_module(_MODULE_NAME)


def _record_run(initial_state=None):
    """Run the pipeline under ARGUS and return (run_id, module)."""
    mod = _pipeline()
    session = ArgusSession(llm_investigation=LLMInvestigationConfig(enabled=False))
    session.set_node_names(list(_ORDER))
    session.set_edges({"fetch": ["transform"], "transform": ["finish"]})

    # Set before execution: the session auto-finalizes once every terminal
    # node has completed, so refs assigned afterwards never reach the record.
    session.node_fn_refs = {n: f"{_MODULE_NAME}:{n}" for n in _ORDER}
    session.node_fn_paths = {n: f"{_MODULE_NAME}.py" for n in _ORDER}

    wrapped = {name: session.wrap(name, getattr(mod, name)) for name in _ORDER}

    state = dict(initial_state or {"query": "hello"})
    for name in _ORDER:
        partial = wrapped[name](state)
        state = {**state, **partial}

    session.finalize()
    return session.run_id, mod


def _step(record, node_name):
    return next(s for s in record.steps if s.node_name == node_name)


# ── the core promise ──────────────────────────────────────────────────────────


@pytest.mark.integration
def test_patched_value_reaches_the_resumed_trajectory():
    run_id, _ = _record_run()
    original = load_run(run_id)
    assert "PLACEHOLDER" in _step(original, "finish").input_state["summary"]

    new_id = ReplayEngine().replay(
        run_id, "transform", patch={"set": {"status": "OK"}}
    )

    replayed = load_run(new_id)
    assert _step(replayed, "transform").output_dict["summary"] == "docs=2 status=OK"
    assert _step(replayed, "finish").output_dict["final"] == "docs=2 status=OK"


@pytest.mark.integration
def test_upstream_nodes_are_not_re_executed():
    run_id, _ = _record_run()

    ReplayEngine().replay(run_id, "transform", patch={"set": {"status": "OK"}})

    # _import_fn reloads the module, so CALLS holds only the replay's calls.
    calls = sys.modules[_MODULE_NAME].CALLS
    assert "fetch" not in calls
    assert calls == ["transform", "finish"]


@pytest.mark.integration
def test_delete_op_reproduces_a_dropped_field_crash():
    """Deleting a field on demand reproduces the exact downstream failure.

    A node crash during replay propagates to the caller (pre-existing
    ReplayEngine behaviour — session.finalize() is not reached), so the
    reproduction surfaces as the original exception rather than a record.
    """
    run_id, _ = _record_run()

    with pytest.raises(KeyError, match="docs"):
        ReplayEngine().replay(run_id, "transform", patch={"delete": ["docs"]})

    assert sys.modules[_MODULE_NAME].CALLS == ["transform"]


# ── provenance ────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_patch_is_persisted_and_round_trips_through_storage():
    run_id, _ = _record_run()
    patch = {"set": {"status": "OK"}}

    new_id = ReplayEngine().replay(run_id, "transform", patch=patch)

    # from the in-process load
    assert load_run(new_id).state_patch == patch
    # and straight off disk, so the JSON schema carries it
    raw = json.loads(Path(f".argus/runs/{new_id}.json").read_text(encoding="utf-8"))
    assert raw["state_patch"] == patch


@pytest.mark.integration
def test_unpatched_replay_records_no_patch():
    run_id, _ = _record_run()
    new_id = ReplayEngine().replay(run_id, "transform")
    assert load_run(new_id).state_patch is None


@pytest.mark.integration
def test_replay_links_back_to_parent_with_patch():
    run_id, _ = _record_run()
    new_id = ReplayEngine().replay(run_id, "transform", patch={"set": {"status": "OK"}})
    replayed = load_run(new_id)
    assert replayed.parent_run_id == run_id
    assert replayed.replay_from_step == "transform"


# ── safety ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_bad_patch_fails_before_any_node_runs():
    run_id, _ = _record_run()

    sys.modules[_MODULE_NAME].CALLS.clear()

    with pytest.raises(PatchError, match="cannot patch input state for node 'transform'"):
        ReplayEngine().replay(run_id, "transform", patch={"set": {"stauts": "OK"}})

    assert sys.modules[_MODULE_NAME].CALLS == []


@pytest.mark.integration
def test_patch_error_names_the_node_and_suggests_the_key():
    run_id, _ = _record_run()
    with pytest.raises(PatchError, match="did you mean 'status'"):
        ReplayEngine().replay(run_id, "transform", patch={"set": {"stauts": "OK"}})


@pytest.mark.integration
def test_original_run_is_left_untouched_on_disk():
    run_id, _ = _record_run()
    before = Path(f".argus/runs/{run_id}.json").read_text(encoding="utf-8")

    ReplayEngine().replay(run_id, "transform", patch={"set": {"status": "OK"}})

    assert Path(f".argus/runs/{run_id}.json").read_text(encoding="utf-8") == before


@pytest.mark.integration
def test_create_missing_gates_new_keys():
    run_id, _ = _record_run()
    patch = {"set": {"brand_new": 1}}

    with pytest.raises(PatchError):
        ReplayEngine().replay(run_id, "transform", patch=patch)

    new_id = ReplayEngine().replay(run_id, "transform", patch=patch, create_missing=True)
    assert load_run(new_id).state_patch == patch


# ── single-node mode ──────────────────────────────────────────────────────────


@pytest.mark.integration
def test_replay_node_applies_patch_in_isolation():
    run_id, _ = _record_run()

    new_id = ReplayEngine().replay_node(
        run_id, "transform", patch={"set": {"status": "OK"}}
    )

    replayed = load_run(new_id)
    assert len(replayed.steps) == 1
    assert replayed.steps[0].output_dict["summary"] == "docs=2 status=OK"
    assert replayed.state_patch == {"set": {"status": "OK"}}
    assert sys.modules[_MODULE_NAME].CALLS == ["transform"]


@pytest.mark.integration
def test_replay_node_rejects_a_bad_patch():
    run_id, _ = _record_run()
    with pytest.raises(PatchError, match="cannot patch input state"):
        ReplayEngine().replay_node(run_id, "transform", patch={"delete": ["nope"]})


# ── HTTP endpoint (step 1.4) ──────────────────────────────────────────────────


@pytest.fixture
def ui_server(tmp_path):
    """Start the real dashboard handler on an ephemeral port."""
    import json as _json
    import threading as _threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from argus.cli.cmd_open_ui import _make_handler

    runs_dir = tmp_path / ".argus" / "runs"
    logs_dir = tmp_path / ".argus" / "logs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    handler = _make_handler(runs_dir, logs_dir, None, tmp_path)
    server = ThreadingHTTPServer(("localhost", 0), handler)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def post(path, payload):
        req = urllib.request.Request(
            f"http://localhost:{port}{path}",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, _json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read() or b"{}")

    def get(path):
        with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=10) as resp:
            return resp.status, _json.loads(resp.read() or b"{}")

    try:
        yield post, get
    finally:
        server.shutdown()
        server.server_close()


def _await_job(get, job_id, timeout=15.0):
    """Poll the replay job until it leaves 'running'."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body = get(f"/api/replay/status/{job_id}")
        if body.get("status") != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


@pytest.mark.integration
def test_api_replay_accepts_a_patch(ui_server):
    post, get = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay",
        {"run_id": run_id, "from_step": "transform", "patch": {"set": {"status": "OK"}}},
    )
    assert status == 202
    assert "job_id" in body

    result = _await_job(get, body["job_id"])
    assert result["status"] == "done", result

    replayed = load_run(result["run_id"])
    assert replayed.state_patch == {"set": {"status": "OK"}}
    assert replayed.parent_run_id == run_id
    assert _step(replayed, "finish").output_dict["final"] == "docs=2 status=OK"


@pytest.mark.integration
def test_api_replay_still_works_without_a_patch(ui_server):
    post, get = ui_server
    run_id, _ = _record_run()

    status, body = post("/api/replay", {"run_id": run_id, "from_step": "transform"})
    assert status == 202

    result = _await_job(get, body["job_id"])
    assert result["status"] == "done", result
    assert load_run(result["run_id"]).state_patch is None


@pytest.mark.integration
def test_api_rejects_a_bad_path_with_the_hint_and_starts_no_job(ui_server):
    from argus.cli import cmd_open_ui

    post, _ = ui_server
    run_id, _ = _record_run()
    jobs_before = len(cmd_open_ui._replay_jobs)

    status, body = post(
        "/api/replay",
        {"run_id": run_id, "from_step": "transform", "patch": {"set": {"stauts": "OK"}}},
    )

    assert status == 400
    assert body["error"] == "invalid_patch"
    assert "did you mean 'status'" in body["message"]
    assert len(cmd_open_ui._replay_jobs) == jobs_before


@pytest.mark.integration
def test_api_rejects_a_malformed_patch_document(ui_server):
    post, _ = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay",
        {"run_id": run_id, "from_step": "transform", "patch": {"bogus_op": {"a": 1}}},
    )
    assert status == 400
    assert "unknown patch op" in body["message"]


@pytest.mark.integration
def test_api_rejects_a_non_object_patch(ui_server):
    post, _ = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay",
        {"run_id": run_id, "from_step": "transform", "patch": ["not", "an", "object"]},
    )
    assert status == 400
    assert body["error"] == "invalid_patch"


@pytest.mark.integration
def test_api_reports_unknown_node_when_patching(ui_server):
    post, _ = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay",
        {"run_id": run_id, "from_step": "nope", "patch": {"set": {"status": "OK"}}},
    )
    assert status == 404
    assert body["error"] == "unknown_node"
    assert "transform" in body["message"]


@pytest.mark.integration
def test_api_create_missing_flag_is_honoured(ui_server):
    post, get = ui_server
    run_id, _ = _record_run()
    payload = {"run_id": run_id, "from_step": "transform", "patch": {"set": {"fresh": 1}}}

    status, _ = post("/api/replay", payload)
    assert status == 400

    status, body = post("/api/replay", {**payload, "create_missing": True})
    assert status == 202
    result = _await_job(get, body["job_id"])
    assert result["status"] == "done", result


@pytest.mark.integration
def test_api_single_node_mode_accepts_a_patch(ui_server):
    post, get = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay",
        {
            "run_id": run_id,
            "from_step": "transform",
            "mode": "node",
            "patch": {"set": {"status": "OK"}},
        },
    )
    assert status == 202

    result = _await_job(get, body["job_id"])
    assert result["status"] == "done", result
    replayed = load_run(result["run_id"])
    assert len(replayed.steps) == 1
    assert replayed.steps[0].output_dict["summary"] == "docs=2 status=OK"


# ── preview endpoint (step 1.5) ───────────────────────────────────────────────


@pytest.mark.integration
def test_preview_returns_changes_and_summary(ui_server):
    post, _ = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay/preview",
        {"run_id": run_id, "from_step": "transform", "patch": {"set": {"status": "OK"}}},
    )

    assert status == 200
    assert body["changes"] == [
        {
            "op": "set",
            "path": "status",
            "before": "PLACEHOLDER",
            "after": "OK",
            "existed": True,
        }
    ]
    assert body["summary"] == ['~ status  "PLACEHOLDER" -> "OK"']


@pytest.mark.integration
def test_preview_runs_nothing_and_changes_nothing(ui_server):
    """A preview that executes a node, or starts a job, is a bug."""
    from argus.cli import cmd_open_ui

    post, _ = ui_server
    run_id, _ = _record_run()
    before = Path(f".argus/runs/{run_id}.json").read_text(encoding="utf-8")
    jobs_before = len(cmd_open_ui._replay_jobs)
    sys.modules[_MODULE_NAME].CALLS.clear()

    status, _ = post(
        "/api/replay/preview",
        {"run_id": run_id, "from_step": "transform", "patch": {"set": {"status": "OK"}}},
    )

    assert status == 200
    assert Path(f".argus/runs/{run_id}.json").read_text(encoding="utf-8") == before
    assert len(cmd_open_ui._replay_jobs) == jobs_before
    assert sys.modules[_MODULE_NAME].CALLS == []


@pytest.mark.integration
def test_preview_reports_a_delete_and_an_added_key(ui_server):
    post, _ = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay/preview",
        {
            "run_id": run_id,
            "from_step": "transform",
            "patch": {"delete": ["docs"], "set": {"brand_new": 5}},
            "create_missing": True,
        },
    )

    assert status == 200
    by_path = {c["path"]: c for c in body["changes"]}
    assert by_path["docs"]["op"] == "delete"
    assert by_path["docs"]["before"] == ["d1", "d2"]
    assert by_path["brand_new"]["existed"] is False
    assert by_path["brand_new"]["after"] == 5


@pytest.mark.integration
def test_preview_caps_large_values(ui_server):
    """The editor calls this on every typing pause — it must not ship megabytes."""
    from argus.cli.cmd_open_ui import _MAX_PREVIEW_VALUE_CHARS

    post, _ = ui_server
    run_id, _ = _record_run()
    blob = "x" * (_MAX_PREVIEW_VALUE_CHARS * 4)

    status, body = post(
        "/api/replay/preview",
        {"run_id": run_id, "from_step": "transform", "patch": {"set": {"status": blob}}},
    )

    assert status == 200
    after = body["changes"][0]["after"]
    assert after["__argus_truncated__"] is True
    assert after["full_length"] > _MAX_PREVIEW_VALUE_CHARS
    assert len(after["preview"]) <= _MAX_PREVIEW_VALUE_CHARS


@pytest.mark.integration
def test_preview_rejects_a_bad_path_with_the_hint(ui_server):
    post, _ = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay/preview",
        {"run_id": run_id, "from_step": "transform", "patch": {"set": {"stauts": "OK"}}},
    )

    assert status == 400
    assert body["error"] == "invalid_patch"
    assert "did you mean 'status'" in body["message"]


@pytest.mark.integration
def test_preview_matches_what_replay_accepts(ui_server):
    """Preview and replay must agree — a preview that lies is worse than none."""
    post, get = ui_server
    run_id, _ = _record_run()
    payload = {"run_id": run_id, "from_step": "transform", "patch": {"set": {"fresh": 1}}}

    preview_status, _ = post("/api/replay/preview", payload)
    replay_status, _ = post("/api/replay", payload)
    assert preview_status == replay_status == 400

    ok_payload = {**payload, "create_missing": True}
    preview_status, _ = post("/api/replay/preview", ok_payload)
    replay_status, replay_body = post("/api/replay", ok_payload)
    assert preview_status == 200
    assert replay_status == 202
    assert _await_job(get, replay_body["job_id"])["status"] == "done"


@pytest.mark.integration
def test_preview_error_cases(ui_server):
    post, _ = ui_server
    run_id, _ = _record_run()

    status, body = post(
        "/api/replay/preview",
        {"run_id": run_id, "from_step": "nope", "patch": {"set": {"status": "OK"}}},
    )
    assert status == 404
    assert body["error"] == "unknown_node"

    status, _ = post(
        "/api/replay/preview",
        {"run_id": "does-not-exist", "from_step": "transform", "patch": {}},
    )
    assert status == 404

    status, _ = post("/api/replay/preview", {"run_id": run_id, "from_step": "transform"})
    assert status == 400  # patch is required
