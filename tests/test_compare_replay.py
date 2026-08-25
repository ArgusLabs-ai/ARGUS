"""VAR-94: explicit resolved / not-resolved verdict on the original failure."""

from __future__ import annotations

import pytest
from conftest import make_event, make_inspection, make_run_record

from argus.correlator import compare_replay


@pytest.mark.unit
def test_compare_replay_original_failure_resolved():
    original = make_run_record(
        events=[
            make_event(
                node_name="retrieve",
                status="fail",
                inspection=make_inspection(
                    missing=["documents"],
                    is_silent=True,
                    severity="critical",
                    message="missing documents",
                ),
            ),
            make_event(node_name="answer", status="fail"),
        ],
        status="silent_failure",
        run_id="orig",
    )
    original.first_failure_step = "retrieve"
    replay = make_run_record(
        events=[
            make_event(node_name="retrieve", status="pass", output={"documents": ["ok"]}),
            make_event(node_name="answer", status="pass"),
        ],
        status="clean",
        run_id="replay",
    )
    impact = compare_replay(replay, original)
    assert impact.original_failure_node == "retrieve"
    assert impact.original_failure_resolved is True
    assert "resolved" in impact.summary.lower()
    assert "retrieve" in impact.summary


@pytest.mark.unit
def test_compare_replay_original_failure_not_resolved():
    original = make_run_record(
        events=[
            make_event(
                node_name="retrieve",
                status="fail",
                inspection=make_inspection(
                    missing=["documents"],
                    is_silent=True,
                    has_tool_failure=False,
                    severity="critical",
                    message="missing documents",
                ),
            )
        ],
        status="silent_failure",
        run_id="orig",
    )
    original.first_failure_step = "retrieve"
    replay = make_run_record(
        events=[
            make_event(
                node_name="retrieve",
                status="fail",
                inspection=make_inspection(
                    missing=["documents"],
                    is_silent=True,
                    severity="critical",
                    message="still missing",
                ),
            )
        ],
        status="silent_failure",
        run_id="replay",
    )
    impact = compare_replay(replay, original)
    assert impact.original_failure_node == "retrieve"
    assert impact.original_failure_resolved is False
    assert "not resolved" in impact.summary.lower()


@pytest.mark.unit
def test_compare_replay_clean_original_has_no_verdict():
    original = make_run_record(events=[make_event()], status="clean", run_id="orig")
    replay = make_run_record(events=[make_event()], status="clean", run_id="replay")
    impact = compare_replay(replay, original)
    assert impact.original_failure_node is None
    assert impact.original_failure_resolved is None
    assert "Original failure" not in impact.summary
