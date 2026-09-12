"""argus ignore — project-level signature suppressions (PRD US-1.3)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from argus import suppressions as sup
from argus.cli.main import app
from argus.models import AnomalySignal, SemanticSignal
from argus.session import ArgusSession
from argus.storage import load_run


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".argus" / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    # Keep the integration probes heuristic-only and deterministic regardless
    # of whether an API key is present in the environment.
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)
    return tmp_path


# ── module ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_add_list_remove_roundtrip(project):
    assert sup.load_suppressions() == []
    assert sup.add_suppression("nl-002") is True
    assert sup.add_suppression("NL-002") is False  # idempotent, case-insensitive
    assert sup.add_suppression("RF-001", node="draft") is True
    items = sup.load_suppressions()
    assert items == [sup.Suppression("NL-002"), sup.Suppression("RF-001", "draft")]
    data = json.loads(sup.config_path().read_text())
    assert data["suppressions"][1] == {"id": "RF-001", "node": "draft"}
    assert sup.remove_suppression("RF-001") is False  # node-scoped ≠ global
    assert sup.remove_suppression("RF-001", node="draft") is True
    assert sup.remove_suppression("RF-001", node="draft") is False


@pytest.mark.unit
def test_matching_and_split(project):
    rules = [sup.Suppression("NL-002"), sup.Suppression("RF-001", "draft")]
    assert sup.is_suppressed("NL-002", "any", rules)
    assert sup.is_suppressed("RF-001", "draft", rules)
    assert not sup.is_suppressed("RF-001", "other", rules)
    sig = lambda i: SemanticSignal(i, "c", "critical", "d", ("out",), "e")  # noqa: E731
    kept, dropped = sup.split_suppressed(
        [sig("NL-002"), sig("PH-007"), sig("RF-001")], "other", rules, id_attr="sig_id"
    )
    assert [s.sig_id for s in kept] == ["PH-007", "RF-001"]
    assert [s.sig_id for s in dropped] == ["NL-002"]


@pytest.mark.unit
def test_config_survives_other_keys(project):
    path = sup.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"other": {"keep": 1}, "suppressions": ["BA-001"]}))
    assert sup.load_suppressions() == [sup.Suppression("BA-001")]
    sup.add_suppression("BA-005")
    data = json.loads(path.read_text())
    assert data["other"] == {"keep": 1}
    assert [s["id"] for s in data["suppressions"]] == ["BA-001", "BA-005"]


# ── session integration ──────────────────────────────────────────────────────


def _first_signal(output):
    """Run a one-node session and return (event, first semantic signal or None)."""
    session = ArgusSession()
    session.set_edges({"gen": []})
    gen = session.wrap("gen", lambda state: output)
    gen({"q": "x"})
    session.finalize()
    record = load_run(session.run_id)
    ev = record.steps[0]
    sigs = ev.inspection.semantic_signals if ev.inspection else []
    return record, ev, (sigs[0] if sigs else None)


@pytest.mark.integration
def test_suppressed_signal_no_longer_changes_status(project):
    # A critical refusal signature (SS-001) — fails the node on its own and is
    # mirrored into a ToolFailure, so it exercises both halves of suppression.
    output = {"summary": "I cannot help with that request."}
    _, before, sig = _first_signal(output)
    if sig is None:
        pytest.skip("no heuristic signature fires on the probe output in this build")
    assert before.status != "pass"

    # Silence the semantic signal and any critical anomaly the same output trips,
    # so the node has nothing left to fail on. A critical semantic signal is
    # mirrored into a ToolFailure; suppressing the signal must drop the twin too,
    # else has_tool_failure keeps failing the node.
    crit_anoms = sorted(a.anomaly_id for a in before.anomaly_signals if a.severity == "critical")
    sup.add_suppression(sig.sig_id)  # new session reads the file
    for aid in crit_anoms:
        sup.add_suppression(aid)
    record, after, sig_after = _first_signal(output)

    assert sig_after is None or sig_after.sig_id != sig.sig_id
    assert after.status == "pass"
    assert record.overall_status == "clean"
    assert [s.sig_id for s in after.suppressed_signals] == [sig.sig_id]
    assert sorted(a.anomaly_id for a in after.suppressed_anomalies) == crit_anoms

    # round-trips through storage
    reloaded = load_run(record.run_id)
    assert reloaded.steps[0].suppressed_signals[0].sig_id == sig.sig_id


@pytest.mark.integration
def test_node_scoped_suppression_only_hits_that_node(project):
    output = {"summary": "I cannot help with that request."}  # SS-001, critical
    _, _, sig = _first_signal(output)
    if sig is None:
        pytest.skip("no heuristic signature fires on the probe output in this build")
    sup.add_suppression(sig.sig_id, node="someone_else")
    _, ev, sig_after = _first_signal(output)
    assert sig_after is not None and sig_after.sig_id == sig.sig_id
    assert ev.status != "pass"
    assert ev.suppressed_signals == []


@pytest.mark.unit
def test_split_anomalies_by_anomaly_id(project):
    rules = [sup.Suppression("BA-005")]
    a = AnomalySignal("BA-005", "warning", 0.5, "flat", "nested", "flat", "")
    b = AnomalySignal("BA-001", "warning", 0.5, "x", "y", "z", "")
    kept, dropped = sup.split_suppressed([a, b], "n", rules, id_attr="anomaly_id")
    assert [x.anomaly_id for x in kept] == ["BA-001"]
    assert [x.anomaly_id for x in dropped] == ["BA-005"]


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_add_list_remove(project):
    r = CliRunner()
    out = r.invoke(app, ["ignore", "nl-002"])
    assert out.exit_code == 0 and "NL-002" in out.output
    out = r.invoke(app, ["ignore", "RF-001", "--node", "draft"])
    assert out.exit_code == 0
    out = r.invoke(app, ["ignore", "--list"])
    assert out.exit_code == 0 and "NL-002" in out.output and "draft" in out.output
    out = r.invoke(app, ["ignore", "RF-001", "--node", "draft", "--remove"])
    assert out.exit_code == 0 and "restored" in out.output
    out = r.invoke(app, ["ignore", "RF-001", "--remove"])
    assert out.exit_code == 1  # not present


@pytest.mark.unit
def test_cli_no_args_lists_and_exits_2(project):
    out = CliRunner().invoke(app, ["ignore"])
    assert out.exit_code == 2
    assert "no suppressions" in out.output


@pytest.mark.unit
def test_doctor_lists_suppressions(project):
    sup.add_suppression("NL-002")
    out = CliRunner().invoke(app, ["doctor"])
    assert "suppressions" in out.output and "NL-002" in out.output
