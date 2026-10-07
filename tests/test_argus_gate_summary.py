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
        {"severity": "critical", "reason": "returned {} | no fields", "suppressed": False},
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
    assert "returned {} \\| no fields" in page, "first critical reason, pipe escaped"
    assert "not this one" not in page
    assert "# Fix: x" in page
    assert "argus-runs" in page
