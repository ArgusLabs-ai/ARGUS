"""VAR-110: terminal findings after invoke(); silence on clean runs."""

from __future__ import annotations

import pytest
from conftest import make_event, make_inspection, make_run_record

from argus.findings import format_run_finding, print_run_finding
from argus.models import ToolFailure
from argus.session import ArgusSession
from argus.storage import load_run


@pytest.mark.unit
def test_format_run_finding_silent_on_clean():
    record = make_run_record(events=[make_event()], status="clean")
    assert format_run_finding(record) is None


@pytest.mark.unit
def test_format_run_finding_silent_failure_missing_dropped_by():
    search = make_event(
        node_name="search",
        status="fail",
        inspection=make_inspection(
            missing=["documents"],
            is_silent=True,
            severity="critical",
            message="missing documents",
        ),
        step_index=0,
    )
    retrieve = make_event(
        node_name="retrieve",
        status="fail",
        inspection=make_inspection(
            missing=["documents"],
            is_silent=True,
            severity="critical",
            message="missing documents",
        ),
        step_index=1,
    )
    record = make_run_record(
        events=[search, retrieve],
        status="silent_failure",
        run_id="20260815-221530-8f3a1c02",
    )
    record.first_failure_step = "retrieve"
    record.root_cause_chain = ["search"]

    text = format_run_finding(record)
    assert text is not None
    assert "[argus] run 8f3a1c02  silent_failure on retrieve" in text
    assert "missing: documents  (dropped by search)" in text
    assert "argus show last" in text
    assert "argus ui" in text


@pytest.mark.unit
def test_format_run_finding_tool_failure_without_missing():
    event = make_event(
        node_name="search",
        status="fail",
        inspection=make_inspection(
            is_silent=True,
            has_tool_failure=True,
            severity="critical",
            tool_failures=[
                ToolFailure(
                    failure_type="empty_result",
                    field_name="results",
                    severity="critical",
                    evidence="empty list",
                )
            ],
        ),
    )
    record = make_run_record(events=[event], status="silent_failure", run_id="abc12345")
    record.first_failure_step = "search"
    text = format_run_finding(record)
    assert text is not None
    assert "silent_failure on search" in text
    assert "empty_result on results" in text


@pytest.mark.unit
def test_format_run_finding_crashed():
    event = make_event(
        node_name="process",
        status="crashed",
        exception="Traceback (most recent call last):\nKeyError: 'documents'",
    )
    record = make_run_record(events=[event], status="crashed", run_id="deadbeef")
    record.first_failure_step = "process"
    text = format_run_finding(record)
    assert text is not None
    assert "crashed on process" in text
    assert "KeyError: 'documents'" in text


@pytest.mark.unit
def test_format_run_finding_interrupted_no_crash_detail():
    event = make_event(node_name="ask_human", status="interrupted")
    record = make_run_record(events=[event], status="interrupted", run_id="pause-01")
    record.interrupt_node = "ask_human"
    text = format_run_finding(record)
    assert text is not None
    assert "interrupted on ask_human" in text
    assert "argus show last" in text


@pytest.mark.unit
def test_print_run_finding_respects_quiet(monkeypatch, capsys):
    event = make_event(node_name="search", status="fail")
    record = make_run_record(events=[event], status="silent_failure")
    record.first_failure_step = "search"
    monkeypatch.setenv("ARGUS_QUIET", "1")
    print_run_finding(record)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.unit
def test_finalize_prints_on_failure(capsys):
    session = ArgusSession()
    session.set_node_names(["search"])
    search = session.wrap("search", lambda s: {"results": []})
    search({})
    session.finalize()

    loaded = load_run(session.run_id)
    assert loaded.overall_status == "silent_failure"
    err = capsys.readouterr().err
    assert "[argus] run" in err
    assert "silent_failure" in err
    assert "argus show last" in err


@pytest.mark.unit
def test_finalize_silent_on_clean(capsys):
    session = ArgusSession()
    session.set_node_names(["fetch"])
    fetch = session.wrap(
        "fetch",
        lambda s: {"data": "The quarterly revenue analysis shows a 15% increase"},
    )
    fetch({})
    session.finalize()

    loaded = load_run(session.run_id)
    assert loaded.overall_status == "clean"
    err = capsys.readouterr().err
    assert "[argus]" not in err


# ── collect_findings: the normalized per-run list ─────────────────────────────

from argus.findings import collect_findings  # noqa: E402
from argus.models import AnomalySignal, SemanticCheckResult, ValidatorResult  # noqa: E402


def _judge_fail(reason="off-topic"):
    return SemanticCheckResult(
        passed=False,
        reason=reason,
        confidence=0.9,
        model="test",
        prompt_tokens=1,
        completion_tokens=1,
        duration_ms=1.0,
    )


@pytest.mark.unit
def test_collect_findings_flattens_every_source():
    search = make_event(
        node_name="search",
        status="fail",
        step_index=0,
        inspection=make_inspection(
            missing=["documents"],
            is_silent=True,
            severity="critical",
            message="missing documents",
            tool_failures=[ToolFailure("rate_limit", "resp", "warning", "HTTP 429")],
        ),
        validator_results=[ValidatorResult("search:nonempty", False, "no hits", "critical")],
        anomaly_signals=[
            AnomalySignal("BA-003", "warning", 0.7, "output 10x smaller", "~1KB", "80B", "")
        ],
        semantic_check=_judge_fail(),
    )
    crash = make_event(
        node_name="rank", status="crashed", step_index=1, exception="KeyError: 'documents'"
    )
    retried = make_event(node_name="rank", status="retried", step_index=2, exception="boom")

    findings = collect_findings([search, crash, retried])

    sources = {f.source for f in findings}
    assert {"heuristic", "validator", "anomaly", "llm", "crash"} <= sources
    # critical first, then warnings
    sev = [f.severity for f in findings]
    assert sev == sorted(sev, key={"critical": 0, "warning": 1, "info": 2}.get)
    # retried steps contribute nothing
    assert not any(f.node == "rank" and f.type != "crash" for f in findings)
    # every reason is a sentence that names the node
    assert all(f.reason.endswith(".") or ":" in f.reason for f in findings)
    assert all(f.node in f.reason for f in findings)


@pytest.mark.unit
def test_collect_findings_ids_are_stable_and_deduped():
    ev = make_event(
        node_name="a",
        status="fail",
        inspection=make_inspection(missing=["x"], is_silent=True, severity="critical"),
    )
    a = collect_findings([ev])
    b = collect_findings([ev])
    assert [f.id for f in a] == [f.id for f in b]
    assert len({f.id for f in a}) == len(a)
    assert a[0].type == "missing_field" and a[0].field_path == "x"


@pytest.mark.unit
def test_collect_findings_empty_on_clean():
    assert collect_findings([make_event(), make_event(node_name="b", step_index=1)]) == []


@pytest.mark.unit
def test_findings_roundtrip_and_backfill(tmp_path, monkeypatch):
    import json

    from argus.storage import save_run

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".argus" / "runs").mkdir(parents=True, exist_ok=True)
    ev = make_event(
        node_name="a",
        status="fail",
        inspection=make_inspection(missing=["x"], is_silent=True, severity="critical"),
    )
    record = make_run_record(events=[ev], status="silent_failure", run_id="rt-1")
    record.findings = collect_findings(record.steps)
    path = save_run(record)
    loaded = load_run("rt-1")
    assert loaded.findings == record.findings and loaded.findings

    # Strip the field to simulate a schema "1" file → back-filled on load.
    data = json.loads(path.read_text())
    data.pop("findings")
    data["schema_version"] = "1"
    path.write_text(json.dumps(data))
    old = load_run("rt-1")
    assert [f.id for f in old.findings] == [f.id for f in record.findings]
