from datetime import datetime, timezone

import pytest

from argus.models import (
    InspectionResult,
    NodeEvent,
    RunRecord,
)


@pytest.fixture(autouse=True)
def tmp_argus_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".argus" / "runs").mkdir(parents=True)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)
    monkeypatch.setattr(
        "argus.session.ArgusSession._sync_shared_signatures",
        staticmethod(lambda: None),
    )
    from argus.registry import reload_registry
    reload_registry()
    return tmp_path


def make_event(
    node_name="node_a",
    status="pass",
    output=None,
    input_state=None,
    step_index=0,
    inspection=None,
    exception=None,
    anomaly_signals=None,
    semantic_check=None,
    duration_ms=100.0,
    validator_results=None,
    attempt_index=0,
):
    return NodeEvent(
        step_index=step_index,
        node_name=node_name,
        status=status,
        input_state=input_state or {},
        output_dict=output,
        duration_ms=duration_ms,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        exception=exception,
        inspection=inspection,
        anomaly_signals=anomaly_signals or [],
        semantic_check=semantic_check,
        validator_results=validator_results or [],
        attempt_index=attempt_index,
    )


def make_inspection(
    missing=None,
    empty=None,
    tool_failures=None,
    signals=None,
    is_silent=False,
    has_tool_failure=False,
    has_tool_warnings=False,
    severity="ok",
    message="All checks passed",
):
    return InspectionResult(
        is_silent_failure=is_silent,
        missing_fields=missing or [],
        empty_fields=empty or [],
        type_mismatches=[],
        severity=severity,
        message=message,
        tool_failures=tool_failures or [],
        has_tool_failure=has_tool_failure,
        has_tool_warnings=has_tool_warnings,
        semantic_signals=signals or [],
    )


def make_run_record(events=None, edges=None, status="clean", run_id="test-run"):
    return RunRecord(
        run_id=run_id,
        argus_version="0.8.11",
        started_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=1000.0,
        overall_status=status,
        first_failure_step=None,
        root_cause_chain=[],
        graph_node_names=[e.node_name for e in (events or [])],
        graph_edge_map=edges or {},
        initial_state={},
        steps=events or [],
    )
