"""Unit tests for argus.correlator — deterministic root-cause correlation layer."""

from __future__ import annotations

import pytest

from argus.correlator import compare_replay, correlate
from argus.models import (
    AnomalySignal,
    InspectionResult,
    NodeEvent,
    RunRecord,
    SemanticSignal,
    ToolFailure,
)


def _make_event(
    step_index: int,
    node_name: str,
    status: str = "pass",
    inspection: InspectionResult | None = None,
    anomaly_signals: list | None = None,
    exception: str | None = None,
    output_dict: dict | None = None,
    input_state: dict | None = None,
) -> NodeEvent:
    return NodeEvent(
        step_index=step_index,
        node_name=node_name,
        status=status,
        input_state=input_state or {},
        output_dict=output_dict or {},
        duration_ms=100.0,
        timestamp_utc="2026-07-06T00:00:00Z",
        exception=exception,
        inspection=inspection,
        anomaly_signals=anomaly_signals or [],
    )


def _make_run(steps, edge_map, overall_status="silent_failure"):
    return RunRecord(
        run_id="test-run",
        argus_version="0.7.1",
        started_at="2026-07-06T00:00:00Z",
        completed_at="2026-07-06T00:00:01Z",
        duration_ms=1000.0,
        overall_status=overall_status,
        first_failure_step=None,
        root_cause_chain=[],
        graph_node_names=[s.node_name for s in steps],
        graph_edge_map=edge_map,
        initial_state={},
        steps=steps,
    )


# ── Origin detection ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_origin_clean_run():
    """Clean run — all nodes pass, no signals → empty origins list, clean summary."""
    steps = [
        _make_event(0, "node_a", status="pass"),
        _make_event(1, "node_b", status="pass"),
    ]
    run = _make_run(steps, {"node_a": ["node_b"]}, overall_status="clean")
    report = correlate(run)

    assert report.degradation_origins == []
    assert report.causal_summary == "No degradation detected — all nodes passed cleanly."


@pytest.mark.unit
def test_origin_single_clean_predecessors():
    """Single origin — node B fails in a chain A->B->C where A is clean
    -> B identified as origin."""
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["score"],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="Missing required field",
    )
    steps = [
        _make_event(0, "node_a", status="pass"),
        _make_event(1, "node_b", status="fail", inspection=insp_b),
        _make_event(2, "node_c", status="pass"),
    ]
    edge_map = {"node_a": ["node_b"], "node_b": ["node_c"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.degradation_origins) == 1
    origin = report.degradation_origins[0]
    assert origin.node_name == "node_b"
    assert origin.confidence >= 0.8
    assert "missing_field" in origin.signal_types


@pytest.mark.unit
def test_origin_crash():
    """Crash origin — a node crashes -> identified as origin, crash type in signal_types."""
    steps = [
        _make_event(0, "node_a", status="pass"),
        _make_event(1, "node_b", status="crashed", exception="RuntimeError: unexpected crash"),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map, overall_status="crashed")
    report = correlate(run)

    assert len(report.degradation_origins) == 1
    origin = report.degradation_origins[0]
    assert origin.node_name == "node_b"
    assert "crash" in origin.signal_types


@pytest.mark.unit
def test_origin_behavioral_only():
    """Behavioral-only origin — anomaly signals only, no structural evidence
    -> confidence capped at 0.40."""
    anomaly = AnomalySignal(
        anomaly_id="BA-001",
        severity="warning",
        suspicion_score=0.8,
        reason="Abnormal response time distribution",
        expected_behavior="normal latency",
        observed_behavior="high latency",
        field_path="",
    )
    steps = [
        _make_event(0, "node_a", status="pass"),
        _make_event(1, "node_b", status="pass", anomaly_signals=[anomaly]),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.degradation_origins) == 1
    origin = report.degradation_origins[0]
    assert origin.node_name == "node_b"
    assert origin.confidence == 0.40
    assert "behavioral anomaly" in origin.reason


@pytest.mark.unit
def test_origin_multiple():
    """Multiple origins — two independent failure points in fan-out graph
    -> both detected, sorted by confidence."""
    tf_critical = ToolFailure(
        failure_type="error_response",
        field_name="api_res",
        severity="critical",
        evidence="500 Internal Error",
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="Critical tool error",
        tool_failures=[tf_critical],
        has_tool_failure=True,
    )
    anom_c = AnomalySignal(
        anomaly_id="BA-001",
        severity="warning",
        suspicion_score=0.8,
        reason="minor variance",
        expected_behavior="exact",
        observed_behavior="slight delta",
        field_path="",
    )
    steps = [
        _make_event(0, "node_a", status="pass"),
        _make_event(1, "node_b", status="fail", inspection=insp_b),
        _make_event(2, "node_c", status="pass", anomaly_signals=[anom_c]),
    ]
    edge_map = {"node_a": ["node_b", "node_c"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.degradation_origins) == 2
    assert report.degradation_origins[0].node_name == "node_b"
    assert report.degradation_origins[1].node_name == "node_c"
    assert report.degradation_origins[0].confidence > report.degradation_origins[1].confidence


@pytest.mark.unit
def test_origin_suppression():
    """Origin suppression — A has structural failures, B (downstream) also fails
    -> B should NOT be separate origin."""
    tf_critical = ToolFailure(
        failure_type="error_response",
        field_name="res",
        severity="critical",
        evidence="API error",
    )
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="Tool failure",
        tool_failures=[tf_critical],
        has_tool_failure=True,
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["score"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="Missing field",
    )
    steps = [
        _make_event(0, "node_a", status="fail", inspection=insp_a),
        _make_event(1, "node_b", status="fail", inspection=insp_b),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.degradation_origins) == 1
    assert report.degradation_origins[0].node_name == "node_a"


@pytest.mark.unit
def test_origin_behavioral_upstream_does_not_suppress_structural_downstream():
    """Behavioral upstream doesn't suppress structural downstream — A has behavioral anomaly,
    B crashes -> B is origin."""
    anomaly = AnomalySignal(
        anomaly_id="BA-001",
        severity="warning",
        suspicion_score=0.8,
        reason="Minor drift",
        expected_behavior="e",
        observed_behavior="o",
        field_path="",
    )
    steps = [
        _make_event(0, "node_a", status="pass", anomaly_signals=[anomaly]),
        _make_event(1, "node_b", status="crashed", exception="RuntimeError: crash"),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map, overall_status="crashed")
    report = correlate(run)

    origin_names = [o.node_name for o in report.degradation_origins]
    assert "node_b" in origin_names


# ── Propagation: Field Drop Cascade ───────────────────────────────────────────


@pytest.mark.unit
def test_field_drop_basic():
    """Basic field drop — node A drops field "score", downstream B has "score" missing
    -> link created confidence 0.85."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["score"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="missing score",
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["score"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="missing score",
    )
    steps = [
        _make_event(0, "node_a", status="pass", output_dict={"score": 100}, inspection=insp_a),
        _make_event(1, "node_b", status="pass", input_state={"other": 1}, inspection=insp_b),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.propagation_chains) == 1
    links = report.propagation_chains[0].links
    assert len(links) == 1
    assert links[0].signal_type == "field_drop"
    assert links[0].confidence == 0.85
    assert "field 'score' dropped at source" in links[0].evidence


@pytest.mark.unit
def test_field_drop_key_error():
    """Field drop causing KeyError — node A drops "score", node B crashes
    with KeyError: 'score' -> link confidence 0.95."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["score"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="missing score",
    )
    steps = [
        _make_event(0, "node_a", status="pass", output_dict={"score": 100}, inspection=insp_a),
        _make_event(1, "node_b", status="crashed", exception="KeyError: 'score'"),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map, overall_status="crashed")
    report = correlate(run)

    links = report.propagation_chains[0].links
    assert len(links) == 1
    assert links[0].signal_type == "field_drop"
    assert links[0].confidence == 0.95
    assert "caused KeyError at target" in links[0].evidence


@pytest.mark.unit
def test_field_drop_not_yet_produced_exclusion():
    """Not-yet-produced exclusion — field "summary" never produced before B flags it missing
    -> NO propagation link."""
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["summary"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="missing summary",
    )
    steps = [
        _make_event(0, "node_a", status="pass", output_dict={"title": "test"}),
        _make_event(1, "node_b", status="pass", inspection=insp_b),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert report.propagation_chains == []


@pytest.mark.unit
def test_field_drop_multi_hop():
    """Multi-hop — A drops field, B and C (both downstream) are affected -> links to both."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["token"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="missing token",
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["token"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="missing token",
    )
    insp_c = InspectionResult(
        is_silent_failure=True,
        missing_fields=["token"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="missing token",
    )
    steps = [
        _make_event(0, "node_a", status="pass", output_dict={"token": "abc"}, inspection=insp_a),
        _make_event(1, "node_b", status="pass", inspection=insp_b),
        _make_event(2, "node_c", status="pass", inspection=insp_c),
    ]
    edge_map = {"node_a": ["node_b"], "node_b": ["node_c"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.propagation_chains) == 1
    chain = report.propagation_chains[0]
    assert chain.nodes[0] == "node_a"
    assert set(chain.nodes) == {"node_a", "node_b", "node_c"}
    assert len(chain.links) == 2


# ── Propagation: Placeholder ──────────────────────────────────────────────────


@pytest.mark.unit
def test_placeholder_direct_text_match():
    """Direct text match — node A PH-* signal evidence text appears in node B input
    -> link confidence 0.90."""
    sig_a = SemanticSignal(
        sig_id="PH-001",
        category="placeholder_outputs",
        severity="warning",
        description="placeholder output",
        field_path=("text",),
        evidence="[INSERT_DATA_HERE]",
    )
    insp_a = InspectionResult(
        is_silent_failure=False,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
        semantic_signals=[sig_a],
    )
    steps = [
        _make_event(0, "node_a", status="pass", inspection=insp_a),
        _make_event(
            1, "node_b", status="pass", input_state={"prompt": "Processing [INSERT_DATA_HERE] now"}
        ),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.propagation_chains) == 1
    link = report.propagation_chains[0].links[0]
    assert link.signal_type == "placeholder"
    assert link.confidence == 0.90


@pytest.mark.unit
def test_placeholder_semantic_collapse():
    """Both nodes have placeholders, no direct match -> link semantic_collapse, confidence 0.70."""
    sig_a = SemanticSignal(
        sig_id="PH-001",
        category="placeholder_outputs",
        severity="warning",
        description="placeholder output A",
        field_path=("text",),
        evidence="[PLACEHOLDER_ALPHA]",
    )
    sig_b = SemanticSignal(
        sig_id="PH-002",
        category="placeholder_outputs",
        severity="warning",
        description="placeholder output B",
        field_path=("text",),
        evidence="[PLACEHOLDER_BETA]",
    )
    insp_a = InspectionResult(
        is_silent_failure=False,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
        semantic_signals=[sig_a],
    )
    insp_b = InspectionResult(
        is_silent_failure=False,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
        semantic_signals=[sig_b],
    )
    steps = [
        _make_event(0, "node_a", status="pass", inspection=insp_a),
        _make_event(1, "node_b", status="pass", input_state={"other": "data"}, inspection=insp_b),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.propagation_chains) == 1
    link = report.propagation_chains[0].links[0]
    assert link.signal_type == "semantic_collapse"
    assert link.confidence == 0.70


@pytest.mark.unit
def test_placeholder_short_evidence_excluded():
    """Short evidence excluded — PH signal with evidence ≤4 chars should be ignored."""
    sig_a = SemanticSignal(
        sig_id="PH-001",
        category="placeholder_outputs",
        severity="warning",
        description="placeholder output",
        field_path=("text",),
        evidence="N/A",  # len 3 <= 4
    )
    insp_a = InspectionResult(
        is_silent_failure=False,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
        semantic_signals=[sig_a],
    )
    steps = [
        _make_event(0, "node_a", status="pass", inspection=insp_a),
        _make_event(1, "node_b", status="pass", input_state={"text": "N/A"}),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert report.propagation_chains == []


# ── Propagation: Anomaly Cascade ──────────────────────────────────────────────


@pytest.mark.unit
def test_anomaly_cascade_matching_ids():
    """Matching anomaly IDs — node A has BA-001 (score > 0.7), node B also has BA-001
    -> link created."""
    anom_a = AnomalySignal(
        anomaly_id="BA-001",
        severity="critical",
        suspicion_score=0.85,
        reason="high latency",
        expected_behavior="fast",
        observed_behavior="slow",
        field_path="",
    )
    anom_b = AnomalySignal(
        anomaly_id="BA-001",
        severity="warning",
        suspicion_score=0.6,
        reason="high latency",
        expected_behavior="fast",
        observed_behavior="slow",
        field_path="",
    )
    steps = [
        _make_event(0, "node_a", status="pass", anomaly_signals=[anom_a]),
        _make_event(1, "node_b", status="pass", anomaly_signals=[anom_b]),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.propagation_chains) == 1
    link = report.propagation_chains[0].links[0]
    assert link.signal_type == "anomaly_cascade"
    assert link.confidence == 0.65


@pytest.mark.unit
def test_anomaly_cascade_non_matching_ids():
    """Non-matching IDs — A has BA-001, B has BA-006 -> NO link."""
    anom_a = AnomalySignal(
        anomaly_id="BA-001",
        severity="critical",
        suspicion_score=0.85,
        reason="high latency",
        expected_behavior="fast",
        observed_behavior="slow",
        field_path="",
    )
    anom_b = AnomalySignal(
        anomaly_id="BA-006",
        severity="critical",
        suspicion_score=0.85,
        reason="format change",
        expected_behavior="json",
        observed_behavior="text",
        field_path="",
    )
    steps = [
        _make_event(0, "node_a", status="pass", anomaly_signals=[anom_a]),
        _make_event(1, "node_b", status="pass", anomaly_signals=[anom_b]),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert report.propagation_chains == []


@pytest.mark.unit
def test_anomaly_cascade_low_suspicion_score():
    """Low suspicion score — A has anomaly with score 0.3
    -> should NOT trigger cascade detection."""
    anom_a = AnomalySignal(
        anomaly_id="BA-001",
        severity="warning",
        suspicion_score=0.3,
        reason="minor variance",
        expected_behavior="exact",
        observed_behavior="slight delta",
        field_path="",
    )
    anom_b = AnomalySignal(
        anomaly_id="BA-001",
        severity="critical",
        suspicion_score=0.9,
        reason="minor variance",
        expected_behavior="exact",
        observed_behavior="slight delta",
        field_path="",
    )
    steps = [
        _make_event(0, "node_a", status="pass", anomaly_signals=[anom_a]),
        _make_event(1, "node_b", status="pass", anomaly_signals=[anom_b]),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert report.propagation_chains == []


# ── Chain assembly ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_chain_assembly_single():
    """Single chain — A->B->C all linked -> one chain with 3 nodes."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["data"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["data"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    insp_c = InspectionResult(
        is_silent_failure=True,
        missing_fields=["data"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    steps = [
        _make_event(0, "node_a", status="pass", output_dict={"data": 1}, inspection=insp_a),
        _make_event(1, "node_b", status="pass", inspection=insp_b),
        _make_event(2, "node_c", status="pass", inspection=insp_c),
    ]
    edge_map = {"node_a": ["node_b"], "node_b": ["node_c"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert len(report.propagation_chains) == 1
    chain = report.propagation_chains[0]
    assert len(chain.nodes) == 3
    assert chain.nodes[0] == "node_a"
    assert set(chain.nodes) == {"node_a", "node_b", "node_c"}


@pytest.mark.unit
def test_chain_classification():
    """Chain classification — all links field_drop -> field_drop_cascade;
    mixed -> mixed_degradation."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["field_x"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["field_x"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    steps = [
        _make_event(0, "node_a", status="pass", output_dict={"field_x": 1}, inspection=insp_a),
        _make_event(1, "node_b", status="pass", inspection=insp_b),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert report.propagation_chains[0].chain_type == "field_drop_cascade"


@pytest.mark.unit
def test_chain_tool_failure_at_origin():
    """Tool failure at origin — origin has tool_failure
    -> chain classified as tool_failure_cascade."""
    tf = ToolFailure(
        failure_type="error_response",
        field_name="res",
        severity="critical",
        evidence="API error",
    )
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["field_x"],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="",
        tool_failures=[tf],
        has_tool_failure=True,
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["field_x"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    steps = [
        _make_event(0, "node_a", status="fail", output_dict={"field_x": 1}, inspection=insp_a),
        _make_event(1, "node_b", status="fail", inspection=insp_b),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert report.propagation_chains[0].chain_type == "tool_failure_cascade"


# ── Timeline ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_timeline_labels_and_ordering():
    """Timeline — labels correct (ORIGIN:, PROPAGATION:, CRASH:, clean),
    events follow step_index order."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["field_x"],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="",
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["field_x"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    steps = [
        _make_event(0, "node_a", status="fail", output_dict={"field_x": 1}, inspection=insp_a),
        _make_event(1, "node_b", status="fail", inspection=insp_b),
        _make_event(2, "node_c", status="crashed", exception="ValueError: invalid argument"),
        _make_event(3, "node_d", status="pass"),
    ]
    edge_map = {"node_a": ["node_b"], "node_b": ["node_c"], "node_c": ["node_d"]}
    run = _make_run(steps, edge_map, overall_status="crashed")
    report = correlate(run)

    timeline = report.timeline
    assert len(timeline) == 4

    assert [t.step_index for t in timeline] == [0, 1, 2, 3]

    assert timeline[0].event_type == "degradation_onset"
    assert timeline[0].label.startswith("ORIGIN:")

    assert timeline[1].event_type == "propagation"
    assert timeline[1].label.startswith("PROPAGATION:")

    assert timeline[2].event_type == "crash"
    assert timeline[2].label.startswith("CRASH:")

    assert timeline[3].event_type == "node_ok"
    assert "clean" in timeline[3].label


# ── Causal summary ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_causal_summary_clean():
    """Clean run -> "No degradation detected — all nodes passed cleanly."."""
    steps = [_make_event(0, "node_a", status="pass")]
    run = _make_run(steps, {}, overall_status="clean")
    report = correlate(run)
    assert report.causal_summary == "No degradation detected — all nodes passed cleanly."


@pytest.mark.unit
def test_causal_summary_behavioral_low_confidence():
    """Behavioral-only with low confidence -> mentions "uncertain" or "may be independent"."""
    anom_a = AnomalySignal(
        anomaly_id="BA-001",
        severity="warning",
        suspicion_score=0.85,
        reason="minor variance",
        expected_behavior="exact",
        observed_behavior="slight delta",
        field_path="",
    )
    anom_b = AnomalySignal(
        anomaly_id="BA-001",
        severity="warning",
        suspicion_score=0.85,
        reason="minor variance",
        expected_behavior="exact",
        observed_behavior="slight delta",
        field_path="",
    )
    steps = [
        _make_event(0, "node_a", status="pass", anomaly_signals=[anom_a]),
        _make_event(1, "node_b", status="pass", anomaly_signals=[anom_b]),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert "uncertain" in report.causal_summary or "may be independent" in report.causal_summary


@pytest.mark.unit
def test_causal_summary_standard_degradation():
    """Standard degradation -> mentions origin node, confidence %, downstream count."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["key"],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="",
    )
    insp_b = InspectionResult(
        is_silent_failure=True,
        missing_fields=["key"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    steps = [
        _make_event(0, "node_a", status="fail", output_dict={"key": "v"}, inspection=insp_a),
        _make_event(1, "node_b", status="fail", inspection=insp_b),
    ]
    edge_map = {"node_a": ["node_b"]}
    run = _make_run(steps, edge_map)
    report = correlate(run)

    assert "Degradation originated at node_a" in report.causal_summary
    assert "1 downstream node(s) affected" in report.causal_summary


# ── Replay comparison (compare_replay) ─────────────────────────────────────────


@pytest.mark.unit
def test_replay_comparison_improvement():
    """Improvement — replay has lower weights -> improved_nodes populated, positive summary."""
    insp_orig = InspectionResult(
        is_silent_failure=True,
        missing_fields=["key1", "key2"],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="",
    )
    orig_steps = [
        _make_event(0, "node_a", status="fail", inspection=insp_orig),
    ]
    replay_steps = [
        _make_event(0, "node_a", status="pass"),
    ]
    orig_run = _make_run(orig_steps, {})
    replay_run = _make_run(replay_steps, {})

    impact = compare_replay(replay_run, orig_run)
    assert impact.improved_nodes == ["node_a"]
    assert impact.regressed_nodes == []
    assert "1 node(s) improved, none regressed" in impact.summary


@pytest.mark.unit
def test_replay_comparison_regression():
    """Regression — replay has higher weights -> regressed_nodes populated, negative summary."""
    insp_replay = InspectionResult(
        is_silent_failure=True,
        missing_fields=["key1", "key2"],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="",
    )
    orig_steps = [
        _make_event(0, "node_a", status="pass"),
    ]
    replay_steps = [
        _make_event(0, "node_a", status="fail", inspection=insp_replay),
    ]
    orig_run = _make_run(orig_steps, {})
    replay_run = _make_run(replay_steps, {})

    impact = compare_replay(replay_run, orig_run)
    assert impact.improved_nodes == []
    assert impact.regressed_nodes == ["node_a"]
    assert "Replay introduced regressions at 1 node(s)" in impact.summary


@pytest.mark.unit
def test_replay_comparison_no_change():
    """No change — same weights -> summary says "no measurable change"."""
    steps = [_make_event(0, "node_a", status="pass")]
    orig_run = _make_run(steps, {})
    replay_run = _make_run(steps, {})

    impact = compare_replay(replay_run, orig_run)
    assert impact.improved_nodes == []
    assert impact.regressed_nodes == []
    assert "Replay produced no measurable change in signal weights." in impact.summary


@pytest.mark.unit
def test_replay_comparison_key_fix_node():
    """Key fix node — improved node with most downstream improvements should be identified."""
    insp_fail = InspectionResult(
        is_silent_failure=True,
        missing_fields=["f1", "f2"],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="",
    )
    orig_steps = [
        _make_event(0, "node_a", status="fail", inspection=insp_fail),
        _make_event(1, "node_b", status="fail", inspection=insp_fail),
        _make_event(2, "node_c", status="fail", inspection=insp_fail),
    ]
    replay_steps = [
        _make_event(0, "node_a", status="pass"),
        _make_event(1, "node_b", status="pass"),
        _make_event(2, "node_c", status="pass"),
    ]
    edge_map = {"node_a": ["node_b", "node_c"]}
    orig_run = _make_run(orig_steps, edge_map)
    replay_run = _make_run(replay_steps, edge_map)

    impact = compare_replay(replay_run, orig_run)
    assert impact.key_fix_node == "node_a"
    assert "Key fix: node_a" in impact.summary


# ── Edge Cases ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_correlate_empty_run():
    """Empty run — steps is empty -> CorrelationReport with 'No steps recorded.'."""
    run = _make_run([], {})
    report = correlate(run)
    assert report.causal_summary == "No steps recorded."
    assert report.degradation_origins == []
    assert report.propagation_chains == []
    assert report.timeline == []


@pytest.mark.unit
def test_correlate_single_node():
    """Single node graph — handles single node run cleanly."""
    steps = [_make_event(0, "single_node", status="pass")]
    run = _make_run(steps, {}, overall_status="clean")
    report = correlate(run)
    assert report.causal_summary == "No degradation detected — all nodes passed cleanly."
    assert len(report.timeline) == 1


@pytest.mark.unit
def test_correlate_disconnected_nodes():
    """Disconnected nodes — graph nodes with no edges handled cleanly."""
    insp_a = InspectionResult(
        is_silent_failure=True,
        missing_fields=["score"],
        empty_fields=[],
        type_mismatches=[],
        severity="warning",
        message="",
    )
    steps = [
        _make_event(0, "node_a", status="fail", inspection=insp_a),
        _make_event(1, "node_b", status="pass"),
    ]
    run = _make_run(steps, {})
    report = correlate(run)

    assert len(report.degradation_origins) == 1
    assert report.degradation_origins[0].node_name == "node_a"
    assert report.propagation_chains == []
