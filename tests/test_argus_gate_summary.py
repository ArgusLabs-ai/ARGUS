"""The GitHub Action's job summary (`.github/actions/argus-gate/summary.py`)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / ".github/actions/argus-gate/summary.py"
_spec = importlib.util.spec_from_file_location("argus_gate_summary", _PATH)
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)

_FAILED = {
    "run_id": "r1",
    "passed": False,
    "overall_status": "silent_failure",
    "root_cause_chain": ["merge_summaries"],
    "findings": [
        {"severity": "warning", "reason": "not this one", "suppressed": False},
        {
            "severity": "critical",
            "type": "tool_error",
            "field_path": "answer.error",
            "reason": "401 invalid api key sk-LIVE-SECRET for jane@example.com",
            "suppressed": False,
        },
    ],
}


@pytest.mark.unit
def test_no_runs_says_the_gate_checked_nothing():
    """A gate that graded nothing passes; the page must not look like a pass."""
    assert "No agent runs were graded" in summary.render([], {})


@pytest.mark.unit
def test_clean_runs_are_one_line():
    page = summary.render([{"run_id": "r1", "passed": True}], {})
    assert "1 agent run(s) graded, all clean" in page


@pytest.mark.unit
def test_a_failed_run_names_the_origin_and_carries_the_fix_prompt():
    page = summary.render([_FAILED, {"run_id": "r2", "passed": True}], {"r1": "# Fix: x"})

    assert "1 of 2 agent run(s) failed" in page
    assert "`merge_summaries`" in page
    assert "`tool_error` on `answer.error`" in page, "first critical finding's type and field"
    assert "sk-LIVE-SECRET" not in page, "a reason can quote recorded values; the page is public"
    assert "not this one" not in page
    assert "# Fix: x" in page
    assert "argus-runs" in page


@pytest.mark.unit
def test_a_verdict_without_a_run_id_is_skipped_not_a_crash(monkeypatch):
    """`argus check` on an unknown id prints an error JSON with no run_id."""

    class _Proc:
        stdout = '{"error": "run not found"}'
        stderr = ""

    monkeypatch.setattr(summary.subprocess, "run", lambda *a, **k: _Proc())
    assert summary._check("nope") is None


@pytest.mark.unit
def test_the_summary_grades_with_the_gates_strict_mode(monkeypatch):
    """Under warn_as_fail the gate fails a warning-only run; the page must not say clean."""
    seen = []

    class _Proc:
        stdout = '{"run_id": "r1", "passed": false}'
        stderr = ""

    def fake_run(cmd, **_):
        seen.append(cmd)
        return _Proc()

    monkeypatch.setattr(summary.subprocess, "run", fake_run)
    monkeypatch.setenv("GATE_STRICT", "warn_as_fail")
    summary._check("r1")
    assert seen[-1][-2:] == ["--strict", "warn_as_fail"]


@pytest.mark.unit
def test_failed_runs_are_bundled_by_full_id(tmp_path, monkeypatch):
    runs = tmp_path / "proj" / ".argus" / "runs"
    runs.mkdir(parents=True)
    (runs / "20261016-r1.json").write_text("{}")
    (runs / "20261016-r2.json").write_text("{}")
    monkeypatch.setenv("ARGUS_DIR", str(tmp_path / "proj"))
    monkeypatch.setenv("ARGUS_BUNDLE", str(tmp_path / "bundle"))

    summary._bundle(["20261016-r1"])

    assert [p.name for p in (tmp_path / "bundle/.argus/runs").iterdir()] == ["20261016-r1.json"]
