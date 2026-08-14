"""VAR-112: project-root run storage, ARGUS_DIR override, UI/doctor discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_event, make_run_record

from argus.cli.ui_serving import format_port_busy_lines, format_ui_startup_lines
from argus.storage import (
    _runs_path,
    argus_dir,
    load_run,
    resolve_project_root,
    save_run,
)


def _git_project(tmp_path: Path, monkeypatch) -> Path:
    """tmp_path as git root; cwd is a nested notebook dir."""
    monkeypatch.delenv("ARGUS_DIR", raising=False)
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    nested = tmp_path / "notebooks"
    nested.mkdir()
    monkeypatch.chdir(nested)
    return tmp_path.resolve()


@pytest.mark.unit
def test_resolve_project_root_prefers_argus_dir(tmp_path, monkeypatch):
    override = tmp_path / "explicit-root"
    override.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("ARGUS_DIR", str(override))
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root() == override.resolve()
    assert _runs_path() == override.resolve() / ".argus" / "runs"


@pytest.mark.unit
def test_resolve_project_root_walks_to_git_root(tmp_path, monkeypatch):
    root = _git_project(tmp_path, monkeypatch)
    assert resolve_project_root() == root
    assert Path.cwd() != root


@pytest.mark.unit
def test_resolve_project_root_walks_to_pyproject(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_project_root() == tmp_path.resolve()


@pytest.mark.unit
def test_runs_path_writes_to_project_root_not_cwd(tmp_path, monkeypatch):
    root = _git_project(tmp_path, monkeypatch)
    runs = _runs_path()
    assert runs == root / ".argus" / "runs"
    assert runs.is_dir()
    assert not (Path.cwd() / ".argus" / "runs").exists()


@pytest.mark.unit
def test_save_and_load_run_agree_when_cwd_is_nested(tmp_path, monkeypatch):
    root = _git_project(tmp_path, monkeypatch)
    record = make_run_record(events=[make_event()], run_id="nested-cwd-run")
    path = save_run(record)
    assert path.parent == root / ".argus" / "runs"
    loaded = load_run("nested-cwd-run")
    assert loaded.run_id == "nested-cwd-run"


@pytest.mark.unit
def test_load_run_prefers_project_root_over_cwd_copy(tmp_path, monkeypatch):
    root = _git_project(tmp_path, monkeypatch)
    record = make_run_record(events=[make_event(node_name="from_root")], run_id="dup-run")
    save_run(record)

    cwd_runs = Path.cwd() / ".argus" / "runs"
    cwd_runs.mkdir(parents=True)
    (cwd_runs / "dup-run.json").write_text(
        json.dumps({"run_id": "dup-run", "steps": [], "overall_status": "clean"}),
        encoding="utf-8",
    )

    loaded = load_run("dup-run")
    assert loaded.steps[0].node_name == "from_root"
    assert (root / ".argus" / "runs" / "dup-run.json").exists()


@pytest.mark.unit
def test_fallback_to_cwd_when_no_project_markers(tmp_path, monkeypatch):
    import shutil

    monkeypatch.delenv("ARGUS_DIR", raising=False)
    leftover = tmp_path / ".argus"
    if leftover.exists():
        shutil.rmtree(leftover)
    bare = tmp_path / "bare-cwd"
    bare.mkdir()
    monkeypatch.chdir(bare)
    assert resolve_project_root() == bare.resolve()
    assert _runs_path() == bare.resolve() / ".argus" / "runs"


@pytest.mark.unit
def test_checkpoints_follow_project_root(tmp_path, monkeypatch):
    from argus.checkpoints import CheckpointRecord, save_checkpoint

    root = _git_project(tmp_path, monkeypatch)
    save_checkpoint(
        CheckpointRecord(
            run_id="cp-1",
            interrupted_at_node="ask",
            checkpoint_state={"n": 1},
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    expected = root / ".argus" / "checkpoints" / "cp-1.json"
    assert expected.exists()
    assert not (Path.cwd() / ".argus" / "checkpoints").exists()


@pytest.mark.unit
def test_argus_dir_matches_runs_parent(tmp_path, monkeypatch):
    root = _git_project(tmp_path, monkeypatch)
    assert argus_dir() == root / ".argus"
    assert _runs_path().parent == argus_dir()


@pytest.mark.unit
def test_doctor_storage_explains_no_runs(tmp_path, monkeypatch):
    from argus.cli.cmd_doctor import _check_storage

    monkeypatch.delenv("ARGUS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    ok, msg = _check_storage()
    assert ok is True
    lower = msg.lower()
    assert "0 runs" in lower or "not yet created" in lower
    assert "attach" in lower
    assert "argus_dir" in lower or "project root" in lower
    assert "cyclic" in lower or "finalize" in lower
    assert "argus-agents" in lower
    assert "[cli]" not in msg
    assert "[langgraph]" not in msg


@pytest.mark.unit
def test_ui_startup_mentions_runs_dir_and_warns_when_empty(tmp_path):
    runs_dir = tmp_path / ".argus" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(format_ui_startup_lines(runs_dir, run_count=0))
    assert str(runs_dir) in text
    assert "0 runs" in text.lower() or "no runs" in text.lower()

    filled = "\n".join(format_ui_startup_lines(runs_dir, run_count=3))
    assert str(runs_dir) in filled
    assert "0 runs" not in filled.lower()


@pytest.mark.unit
def test_ui_warns_when_port_serves_a_different_project(tmp_path):
    ours = tmp_path / "proj-a" / ".argus" / "runs"
    theirs = tmp_path / "proj-b" / ".argus" / "runs"
    text = "\n".join(
        format_port_busy_lines(
            ours,
            {"project_root": str(tmp_path / "proj-b"), "runs_dir": str(theirs)},
        )
    )
    assert "7842" in text or "already running" in text.lower()
    assert str(ours) in text
    assert str(theirs) in text
    assert "different" in text.lower()

    same = "\n".join(
        format_port_busy_lines(
            ours,
            {"project_root": str(tmp_path / "proj-a"), "runs_dir": str(ours)},
        )
    )
    assert "different" not in same.lower()
    assert str(ours) in same
