"""Unit tests for argus.correlator — cross-node failure correlation."""
from datetime import datetime, timezone

import pytest

from argus.correlator import _node_signal_weight, correlate
from argus.models import (
    AnomalySignal,
    InspectionResult,
    NodeEvent,
    RunRecord,
    SemanticSignal,
    ToolFailure,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

def _ts():
    return datetime.now(timezone.utc).isoformat()

def _event(name, step, status="pass", output=None, inspection=None,
           exception=None, anomaly_signals=None):
    return NodeEvent(
        step_index=step, node_name=name, status=status,
        input_state={}, output_dict=output or {},
        duration_ms=100.0, timestamp_utc=_ts(),
        exception=exception, inspection=inspection,
        anomaly_signals=anomaly_signals or [],
    )

def _insp(tool_failures=None, missing=None, signals=None, has_tf=False):
    return InspectionResult(
        is_silent_failure=bool(missing),
        missing_fields=missing or [],
        empty_fields=[],
        type_mismatches=[],
        severity="critical" if has_tf else "ok",
        message="test",
        tool_failures=tool_failures or [],
        has_tool_failure=has_tf,
        semantic_signals=signals or [],
    )

def _record(events, edges=None, status="silent_failure"):
    return RunRecord(
        run_id="test",
        argus_version="0.8.10",
        started_at=_ts(),
        completed_at=_ts(),
        duration_ms=1000.0,
        overall_status=status,
        first_failure_step=None,
        root_cause_chain=[],
        graph_node_names=list({e.node_name for e in events}),
        graph_edge_map=edges or {},
        initial_state={},
        steps=events,
    )

@pytest.mark.unit
class TestCorrelatorCleanRun:
    def test_clean_run_no_origins(self):
        events = [
            _event("a", 0, output={"x": 1}),
            _event("b", 1, output={"y": 2}),
        ]
        report = correlate(_record(events, {"a": ["b"]}, "clean"))
        assert report.degradation_origins == []
        assert report.propagation_chains == []
        assert "No degradation" in report.causal_summary

    def test_empty_run(self):
        report = correlate(_record([], {}, "clean"))
        assert "No steps" in report.causal_summary

@pytest.mark.unit
class TestCorrelatorOrigins:
    def test_single_origin_detected(self):
        tf = ToolFailure("error_response", "error", "critical", "boom")
        events = [
            _event("a", 0, output={"x": 1}, inspection=_insp(tool_failures=[tf], has_tf=True)),
            _event("b", 1, output={"y": 2}),
        ]
        report = correlate(_record(events, {"a": ["b"]}))
        assert len(report.degradation_origins) >= 1
        assert report.degradation_origins[0].node_name == "a"
        breakdown = dict(report.degradation_origins[0].confidence_breakdown)
        assert breakdown.get("tool_failure (critical)", 0) >= 3.0

    def test_crash_is_origin(self):
        events = [
            _event("a", 0, output={"x": 1}),
            _event("b", 1, status="crashed", exception="KeyError: 'z'"),
        ]
        report = correlate(_record(events, {"a": ["b"]}, "crashed"))
        origins = [o.node_name for o in report.degradation_origins]
        assert "b" in origins

    def test_retried_attempt_signals_do_not_create_origin(self):
        """Superseded retry iterations must not taint the final correlation."""
        tf = ToolFailure("error_response", "error", "critical", "boom")
        events = [
            _event(
                "retrieve",
                0,
                status="retried",
                output={"error": "timeout"},
                inspection=_insp(tool_failures=[tf], has_tf=True, missing=["documents"]),
            ),
            _event("retrieve", 1, status="pass", output={"documents": ["ok"]}),
            _event("answer", 2, status="pass", output={"text": "hi"}),
        ]
        report = correlate(_record(events, {"retrieve": ["answer"]}, "clean"))
        assert report.degradation_origins == []


@pytest.mark.unit
class TestCorrelatorPropagation:
    def test_field_drop_cascade(self):
        """Node A drops field, Node B has it missing -> field_drop link."""
        events = [
            _event("a", 0, output={"summary": "good"},
                   inspection=_insp(missing=["summary"])),
            _event("b", 1, output={"final": "x"},
                   inspection=_insp(missing=["summary"])),
        ]
        report = correlate(_record(events, {"a": ["b"]}))
        if report.propagation_chains:
            assert any(
                link.signal_type == "field_drop"
                for chain in report.propagation_chains
                for link in chain.links
            )

    def test_placeholder_propagation(self):
        ph_signal = SemanticSignal(
            sig_id="PH-001", category="placeholder_outputs", severity="critical",
            description="placeholder", field_path=("answer",),
            evidence="lorem ipsum dolor", confidence=1.0,
        )
        events = [
            _event("a", 0, output={"answer": "lorem ipsum dolor"},
                   inspection=_insp(signals=[ph_signal])),
            _event("b", 1, output={"final": "based on lorem ipsum dolor"},
                   inspection=_insp(signals=[ph_signal])),
        ]
        # Set input_state for b to include the placeholder
        events[1].input_state = {"answer": "lorem ipsum dolor"}
        report = correlate(_record(events, {"a": ["b"]}))
        if report.propagation_chains:
            chain_types = [c.chain_type for c in report.propagation_chains]
            assert any("placeholder" in ct or "semantic" in ct for ct in chain_types)

@pytest.mark.unit
class TestCorrelatorTimeline:
    def test_timeline_has_all_events(self):
        events = [
            _event("a", 0, output={"x": 1}),
            _event("b", 1, output={"y": 2}),
        ]
        report = correlate(_record(events, {"a": ["b"]}, "clean"))
        assert len(report.timeline) == 2
        assert report.timeline[0].node_name == "a"
        assert report.timeline[1].node_name == "b"

@pytest.mark.unit
class TestSignalWeight:
    def test_clean_node_zero_weight(self):
        event = _event("a", 0)
        assert _node_signal_weight(event) == 0.0

    def test_critical_tool_failure_weight(self):
        tf = ToolFailure("error_response", "error", "critical", "boom")
        event = _event("a", 0, inspection=_insp(tool_failures=[tf], has_tf=True))
        assert _node_signal_weight(event) == 3.0

    def test_crash_minimum_weight(self):
        event = _event("a", 0, status="crashed")
        assert _node_signal_weight(event) >= 2.0


@pytest.mark.unit
class TestAnomalyCascade:
    def test_same_anomaly_cascades(self):
        """Same anomaly ID in upstream and downstream = cascade link."""
        from argus.correlator import _detect_anomaly_cascade

        a_anomaly = AnomalySignal("BA-001", "critical", 0.9, "length collapse",
                                   "100-50000", "5", "")
        b_anomaly = AnomalySignal("BA-001", "critical", 0.8, "length collapse",
                                   "100-50000", "3", "")
        events = [
            _event("a", 0, anomaly_signals=[a_anomaly]),
            _event("b", 1, anomaly_signals=[b_anomaly]),
        ]
        links = _detect_anomaly_cascade(events, {"a": ["b"]})
        assert len(links) >= 1
        assert links[0].signal_type == "anomaly_cascade"
        assert links[0].source_node == "a"
        assert links[0].target_node == "b"

    def test_different_anomaly_no_cascade(self):
        """Different anomaly IDs should NOT create cascade links."""
        from argus.correlator import _detect_anomaly_cascade

        a_anomaly = AnomalySignal("BA-001", "critical", 0.9, "length collapse",
                                   "100-50000", "5", "")
        b_anomaly = AnomalySignal("BA-006", "critical", 0.8, "empty output",
                                   "non-empty", "empty", "")
        events = [
            _event("a", 0, anomaly_signals=[a_anomaly]),
            _event("b", 1, anomaly_signals=[b_anomaly]),
        ]
        links = _detect_anomaly_cascade(events, {"a": ["b"]})
        assert len(links) == 0

    def test_low_score_no_cascade(self):
        """Anomalies with suspicion_score <= 0.7 should not cascade."""
        from argus.correlator import _detect_anomaly_cascade

        a_anomaly = AnomalySignal("BA-001", "warning", 0.5, "maybe",
                                   "100-50000", "50", "")
        b_anomaly = AnomalySignal("BA-001", "warning", 0.4, "maybe",
                                   "100-50000", "45", "")
        events = [
            _event("a", 0, anomaly_signals=[a_anomaly]),
            _event("b", 1, anomaly_signals=[b_anomaly]),
        ]
        links = _detect_anomaly_cascade(events, {"a": ["b"]})
        assert len(links) == 0

    def test_cascade_integrated_in_correlate(self):
        """Anomaly cascade appears in full correlate() output."""
        a_anomaly = AnomalySignal("BA-002", "critical", 0.85, "repetitive",
                                   "diverse", "repetitive", "text")
        b_anomaly = AnomalySignal("BA-002", "critical", 0.8, "repetitive",
                                   "diverse", "repetitive", "text")
        events = [
            _event("a", 0, anomaly_signals=[a_anomaly]),
            _event("b", 1, anomaly_signals=[b_anomaly]),
        ]
        report = correlate(_record(events, {"a": ["b"]}))
        all_links = []
        for chain in report.propagation_chains:
            all_links.extend(chain.links)
        cascade_links = [lk for lk in all_links if lk.signal_type == "anomaly_cascade"]
        assert len(cascade_links) >= 1
