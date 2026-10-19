"""``pytest --argus-report-only``: record, grade and save runs like ``--argus``, never flip a test.

A team workspace applies its own policy to the runs a job uploads, so the job must not turn red
because a run was not clean. The plugin also keeps a list of the run ids a session saved
(``.argus/session_runs.txt``), which ``argus cloud upload`` reads: it is a separate process, so
the per-test captures in ``argus.run_context`` are gone by then.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import make_run_record

from argus import run_context, storage

pytest_plugins = ["pytester"]

_GRAPH = """
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def _build(failing):
    def api_call(state):
        if failing:
            return {"results": [], "error": "Connection refused", "status_code": 503}
        return {"results": ["a"], "n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", lambda state: {"n": 1})
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    return g.compile()
"""
_CLEAN = _GRAPH + "\ndef test_clean():\n    _build(False).invoke({'n': 0})\n"
_SILENT = _GRAPH + "\ndef test_silent():\n    _build(True).invoke({'n': 0})\n"
_BOTH = _GRAPH + (
    "\ndef test_clean():\n    _build(False).invoke({'n': 0})\n"
    "\ndef test_silent():\n    _build(True).invoke({'n': 0})\n"
)
_TEST_FAILS_ITSELF = _GRAPH + (
    "\ndef test_broken():\n    _build(False).invoke({'n': 0})\n    assert 1 == 2\n"
)


def _project(pytester: pytest.Pytester) -> Path:
    pytester.makefile(".toml", pyproject="[project]\nname = 'demo'\n")
    runs = pytester.path / ".argus" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    return runs


def _statuses(runs: Path) -> list[str]:
    files = sorted(runs.glob("*.json"))
    return sorted(json.loads(f.read_text(encoding="utf-8"))["overall_status"] for f in files)


@pytest.fixture(autouse=True)
def _no_report_only_env(monkeypatch):
    monkeypatch.delenv("ARGUS_REPORT_ONLY", raising=False)


# ── the switch ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_report_only_never_fails_a_test_for_a_run_that_was_not_clean(pytester):
    runs = _project(pytester)
    pytester.makepyfile(_SILENT)
    result = pytester.runpytest("--argus-report-only", "-q")
    result.assert_outcomes(passed=1)
    assert "argus check failed" not in str(result.stdout)
    assert _statuses(runs) == ["silent_failure"]  # recorded and graded all the same


@pytest.mark.unit
def test_without_report_only_the_same_test_fails(pytester):
    runs = _project(pytester)
    pytester.makepyfile(_SILENT)
    pytester.runpytest("--argus", "-q").assert_outcomes(failed=1)
    assert _statuses(runs) == ["silent_failure"]


@pytest.mark.unit
@pytest.mark.parametrize("args", [["--argus-report-only"], ["--argus", "--argus-report-only"]])
def test_report_only_with_or_without_argus_records_clean_and_silent_runs(pytester, args):
    runs = _project(pytester)
    pytester.makepyfile(_BOTH)
    pytester.runpytest(*args, "-q").assert_outcomes(passed=2)
    assert _statuses(runs) == ["clean", "silent_failure"]


@pytest.mark.unit
@pytest.mark.parametrize("extra", [[], ["--argus"]])
@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_the_environment_variable_is_the_same_switch(pytester, monkeypatch, extra, value):
    runs = _project(pytester)
    monkeypatch.setenv("ARGUS_REPORT_ONLY", value)
    pytester.makepyfile(_SILENT)
    pytester.runpytest(*extra, "-q").assert_outcomes(passed=1)
    assert _statuses(runs) == ["silent_failure"]


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_an_environment_variable_that_is_not_on_does_not_turn_it_on(pytester, monkeypatch, value):
    runs = _project(pytester)
    monkeypatch.setenv("ARGUS_REPORT_ONLY", value)
    pytester.makepyfile(_SILENT)
    pytester.runpytest("-q").assert_outcomes(passed=1)
    assert _statuses(runs) == []  # the plugin is inert without a switch
    pytester.runpytest("--argus", "-q").assert_outcomes(failed=1)


@pytest.mark.unit
def test_report_only_does_not_hide_a_test_that_fails_for_its_own_reasons(pytester):
    runs = _project(pytester)
    pytester.makepyfile(_TEST_FAILS_ITSELF)
    pytester.runpytest("--argus-report-only", "-q").assert_outcomes(failed=1)
    assert _statuses(runs) == ["clean"]


@pytest.mark.unit
def test_the_report_only_state_ends_with_the_session(pytester):
    _project(pytester)
    pytester.makepyfile(_CLEAN)
    pytester.runpytest("--argus-report-only", "-q").assert_outcomes(passed=1)
    assert run_context.report_only() is False
    assert run_context.session_file() is None


# ── the list of runs a session saved ─────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("args", [["--argus-report-only"], ["--argus"]])
def test_the_session_file_lists_every_run_the_session_saved(pytester, args):
    runs = _project(pytester)
    pytester.makepyfile(_BOTH)
    pytester.runpytest(*args, "-q")
    listed = (pytester.path / ".argus" / "session_runs.txt").read_text(encoding="utf-8").split()
    assert sorted(listed) == sorted(f.stem for f in runs.glob("*.json")) and len(listed) == 2


@pytest.mark.unit
def test_each_session_starts_a_fresh_list(pytester):
    runs = _project(pytester)
    listing = pytester.path / ".argus" / "session_runs.txt"
    listing.write_text("20250101-000000-stale\n", encoding="utf-8")  # an earlier job's
    pytester.makepyfile(_CLEAN)
    pytester.runpytest("--argus-report-only", "-q").assert_outcomes(passed=1)
    assert listing.read_text(encoding="utf-8").split() == [f.stem for f in runs.glob("*.json")]


@pytest.mark.unit
def test_a_session_with_no_runs_leaves_an_empty_list_not_no_list(pytester):
    _project(pytester)
    pytester.makepyfile("def test_nothing():\n    assert True\n")
    pytester.runpytest("--argus-report-only", "-q").assert_outcomes(passed=1)
    assert (pytester.path / ".argus" / "session_runs.txt").read_text(encoding="utf-8") == ""


@pytest.mark.unit
def test_without_a_switch_no_list_is_made(pytester):
    _project(pytester)
    pytester.makepyfile(_CLEAN)
    pytester.runpytest("-q").assert_outcomes(passed=1)
    assert not (pytester.path / ".argus" / "session_runs.txt").exists()


@pytest.mark.unit
def test_parallel_workers_all_add_to_the_one_list(pytester):
    pytest.importorskip("xdist")
    runs = _project(pytester)
    pytester.makepyfile(test_a=_CLEAN, test_b=_SILENT)
    result = pytester.runpytest("--argus-report-only", "-n", "2", "--dist=loadfile", "-q")
    result.assert_outcomes(passed=2)
    listed = (pytester.path / ".argus" / "session_runs.txt").read_text(encoding="utf-8").split()
    assert sorted(listed) == sorted(f.stem for f in runs.glob("*.json")) and len(listed) == 2


# ── run_context: the list itself ─────────────────────────────────────────────


@pytest.fixture
def listing(tmp_path):
    path = tmp_path / "list" / "session_runs.txt"
    yield path
    run_context.end_session()


@pytest.mark.unit
def test_a_run_is_listed_whether_or_not_a_test_is_capturing(listing):
    run_context.begin_session(listing, fresh=True)
    run_context.record_run_id("a")
    capture = run_context.begin_run_capture()
    try:
        run_context.record_run_id("b")
        run_context.record_run_id("a")  # saved twice: listed once when read
    finally:
        run_context.end_run_capture(capture)
    assert run_context.session_run_ids(listing) == ["a", "b"]
    assert run_context.captured_run_ids(capture) == {"a", "b"}  # the per-test capture is unchanged


@pytest.mark.unit
def test_nothing_is_listed_when_no_session_was_begun(listing):
    run_context.record_run_id("a")
    assert run_context.session_run_ids(listing) is None
    assert not listing.exists()


@pytest.mark.unit
def test_a_worker_adds_to_the_list_and_never_truncates_it(listing):
    run_context.begin_session(listing, fresh=True)
    run_context.record_run_id("first")
    run_context.end_session()
    run_context.begin_session(listing, fresh=False)
    run_context.record_run_id("second")
    assert run_context.session_run_ids(listing) == ["first", "second"]
    run_context.begin_session(listing, fresh=True)
    assert run_context.session_run_ids(listing) == []


@pytest.mark.unit
def test_a_list_that_cannot_be_read_is_not_a_list(tmp_path):
    assert run_context.session_run_ids(tmp_path / "missing.txt") is None
    assert run_context.session_run_ids(tmp_path) is None  # a directory


@pytest.mark.unit
def test_blank_lines_and_spaces_are_not_run_ids(listing):
    listing.parent.mkdir(parents=True)
    listing.write_text("a\n\n  b  \n\t\nc\n", encoding="utf-8")
    assert run_context.session_run_ids(listing) == ["a", "b", "c"]


@pytest.mark.unit
def test_a_list_that_cannot_be_written_never_breaks_saving_a_run(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    run_context.begin_session(blocker / "session_runs.txt", fresh=True)  # cannot be made
    try:
        with caplog.at_level("WARNING", logger="argus"):
            run_context.record_run_id("a")  # nothing raised
    finally:
        run_context.end_session()
    assert any("session" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_processes_append_to_one_list_without_losing_lines(listing):
    run_context.begin_session(listing, fresh=True)
    run_context.end_session()
    code = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from argus import run_context
        run_context.begin_session(Path(sys.argv[1]), fresh=False)
        for i in range(200):
            run_context.record_run_id(f"{sys.argv[2]}-{i:03d}")
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(listing), name]) for name in "abcd"]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    ids = run_context.session_run_ids(listing)
    assert ids is not None and len(ids) == 800 and len(set(ids)) == 800


@pytest.mark.unit
def test_save_run_lists_the_run_it_saves(listing, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_runs_path", lambda: tmp_path)
    run_context.begin_session(listing, fresh=True)
    storage.save_run(make_run_record(run_id="r1"))
    assert run_context.session_run_ids(listing) == ["r1"]


@pytest.mark.unit
def test_the_session_file_sits_in_the_projects_argus_directory(tmp_path):
    assert storage.session_path() == tmp_path / ".argus" / "session_runs.txt"
