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
