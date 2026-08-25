"""Tests for tool_chain_analyzer — covers all four detectors."""

from __future__ import annotations

import pytest

from argus.models import NodeEvent, RunRecord
from argus.tool_chain_analyzer import analyze_tool_chains


def _make_event(
    node_name: str,
    step_index: int,
    input_state: dict | None = None,
    output_dict: dict | None = None,
    status: str = "pass",
    attempt_index: int = 0,
) -> NodeEvent:
    return NodeEvent(
        step_index=step_index,
        node_name=node_name,
        status=status,
        input_state=input_state or {},
        output_dict=output_dict,
        duration_ms=100.0,
        timestamp_utc="2025-01-01T00:00:00Z",
        attempt_index=attempt_index,
    )


def _make_record(
    steps: list[NodeEvent],
    edge_map: dict[str, list[str]] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id="test-run",
        argus_version="0.0.0",
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:00:01Z",
        duration_ms=1000.0,
        overall_status="clean",
        first_failure_step=None,
        root_cause_chain=[],
        graph_node_names=[e.node_name for e in steps],
        graph_edge_map=edge_map or {},
        initial_state={},
        steps=steps,
    )


@pytest.mark.unit
def test_clean_run_no_findings():
    steps = [
        _make_event("fetch", 0, output_dict={"data": "hello"}),
        _make_event("process", 1, output_dict={"result": "done"}),
    ]
    record = _make_record(steps, {"fetch": ["process"]})
    findings = analyze_tool_chains(record)
    assert findings == []


@pytest.mark.unit
def test_retry_storm_detected():
    """TC-001: Same node called 4 times with similar inputs."""
    query = {"query": "best restaurants in NYC", "limit": 10}
    steps = [
        _make_event("search", i, input_state=query, output_dict={"results": []}, attempt_index=i)
        for i in range(4)
    ]
    record = _make_record(steps)
    findings = analyze_tool_chains(record)

    tc001 = [f for f in findings if f.finding_id == "TC-001"]
    assert len(tc001) == 1
    assert tc001[0].finding_type == "retry_storm"
    assert tc001[0].severity == "warning"  # 4 < 5
    assert "search" in tc001[0].nodes_involved


@pytest.mark.unit
def test_retry_storm_critical_at_5():
    """TC-001: 5+ retries should be critical severity."""
    query = {"q": "test query"}
    steps = [
        _make_event("search", i, input_state=query, output_dict={"r": []}, attempt_index=i)
        for i in range(6)
    ]
    record = _make_record(steps)
    findings = analyze_tool_chains(record)

    tc001 = [f for f in findings if f.finding_id == "TC-001"]
    assert len(tc001) == 1
    assert tc001[0].severity == "critical"


@pytest.mark.unit
def test_no_retry_storm_with_different_inputs():
    """TC-001: Different inputs across calls should not trigger."""
    queries = [
        {"query": "what is the capital of France", "lang": "en"},
        {"topic": "machine learning algorithms", "depth": 5},
        {"search": "pizza recipe with mushrooms and olives", "region": "IT"},
        {"url": "https://example.com/api/data", "method": "POST"},
    ]
    steps = [
        _make_event("search", i, input_state=queries[i], output_dict={"r": []})
        for i in range(4)
    ]
    record = _make_record(steps)
    findings = analyze_tool_chains(record)
    tc001 = [f for f in findings if f.finding_id == "TC-001"]
    assert len(tc001) == 0


@pytest.mark.unit
def test_ordering_anomaly_detected():
    """TC-002: Successor node runs before predecessor."""
    steps = [
        _make_event("summarize", 0, output_dict={"summary": "x"}),
        _make_event("search", 1, output_dict={"results": ["a"]}),
    ]
    edge_map = {"search": ["summarize"]}  # search should come first
    record = _make_record(steps, edge_map)
    findings = analyze_tool_chains(record)

    tc002 = [f for f in findings if f.finding_id == "TC-002"]
    assert len(tc002) == 1
    assert tc002[0].finding_type == "ordering_anomaly"


@pytest.mark.unit
def test_no_ordering_anomaly_on_retry_cycle():
    """TC-002: generate → validate → generate is intentional, not a violation."""
    steps = [
        _make_event("generate", 0, output_dict={"draft": "v1"}, attempt_index=0),
        _make_event("validate", 1, output_dict={"ok": False}, attempt_index=0),
        _make_event("generate", 2, output_dict={"draft": "v2"}, attempt_index=1, status="pass"),
        _make_event("validate", 3, output_dict={"ok": True}, attempt_index=1),
    ]
    edge_map = {"generate": ["validate"], "validate": ["generate"]}
    record = _make_record(steps, edge_map)
    findings = analyze_tool_chains(record)
    tc002 = [f for f in findings if f.finding_id == "TC-002"]
    assert tc002 == []


@pytest.mark.unit
def test_no_ordering_anomaly_correct_order():
    """TC-002: Correct execution order should not trigger."""
    steps = [
        _make_event("search", 0, output_dict={"results": ["a"]}),
        _make_event("summarize", 1, output_dict={"summary": "x"}),
    ]
    edge_map = {"search": ["summarize"]}
    record = _make_record(steps, edge_map)
    findings = analyze_tool_chains(record)
    tc002 = [f for f in findings if f.finding_id == "TC-002"]
    assert len(tc002) == 0


@pytest.mark.unit
def test_unused_result_detected():
    """TC-003: Source output not reflected in target output."""
    steps = [
        _make_event("fetch_data", 0, output_dict={
            "documents": ["doc1 content here is a long enough string to pass the threshold"] * 3,
            "metadata": {"count": 3},
        }),
        _make_event("generate", 1, output_dict={
            "answer": "I generated something completely unrelated to the fetched data",
            "confidence": 0.9,
        }),
    ]
    edge_map = {"fetch_data": ["generate"]}
    record = _make_record(steps, edge_map)
    findings = analyze_tool_chains(record)

    tc003 = [f for f in findings if f.finding_id == "TC-003"]
    assert len(tc003) == 1
    assert tc003[0].finding_type == "unused_result"


@pytest.mark.unit
def test_no_unused_result_with_key_overlap():
    """TC-003: Overlapping keys means data is used."""
    steps = [
        _make_event("fetch", 0, output_dict={
            "results": ["data"] * 20,
            "query": "test",
        }),
        _make_event("process", 1, output_dict={
            "results": ["processed"],
            "summary": "done",
        }),
    ]
    edge_map = {"fetch": ["process"]}
    record = _make_record(steps, edge_map)
    findings = analyze_tool_chains(record)
    tc003 = [f for f in findings if f.finding_id == "TC-003"]
    assert len(tc003) == 0


@pytest.mark.unit
def test_argument_degradation_detected():
    """TC-004: Input queries shrink across retries."""
    long_query = (
        "best Italian restaurants in downtown Manhattan "
        "New York City with outdoor seating"
    )
    steps = [
        _make_event("search", 0,
                     input_state={"query": long_query},
                     output_dict={"r": []}, attempt_index=0),
        _make_event("search", 1,
                     input_state={"query": "restaurants Manhattan"},
                     output_dict={"r": []}, attempt_index=1),
        _make_event("search", 2,
                     input_state={"query": "food"},
                     output_dict={"r": []}, attempt_index=2),
    ]
    record = _make_record(steps)
    findings = analyze_tool_chains(record)

    tc004 = [f for f in findings if f.finding_id == "TC-004"]
    assert len(tc004) == 1
    assert tc004[0].finding_type == "argument_degradation"


@pytest.mark.unit
def test_no_argument_degradation_stable_inputs():
    """TC-004: Stable input sizes should not trigger."""
    steps = [
        _make_event("search", 0,
                     input_state={"query": "best restaurants in NYC"},
                     output_dict={"r": []}, attempt_index=0),
        _make_event("search", 1,
                     input_state={"query": "top restaurants in NYC"},
                     output_dict={"r": []}, attempt_index=1),
    ]
    record = _make_record(steps)
    findings = analyze_tool_chains(record)
    tc004 = [f for f in findings if f.finding_id == "TC-004"]
    assert len(tc004) == 0


@pytest.mark.unit
def test_sentinel_nodes_ignored():
    """__start__ and __end__ nodes should not produce findings."""
    steps = [
        _make_event("__start__", 0, output_dict={"x": "y"}),
        _make_event("process", 1, output_dict={"result": "done"}),
        _make_event("__end__", 2, output_dict={"final": "output"}),
    ]
    edge_map = {"__start__": ["process"], "process": ["__end__"]}
    record = _make_record(steps, edge_map)
    findings = analyze_tool_chains(record)
    assert all("__start__" not in f.nodes_involved for f in findings)
    assert all("__end__" not in f.nodes_involved for f in findings)
