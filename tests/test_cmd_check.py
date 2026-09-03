"""VAR-113: argus check CI gate and pytest --argus for instrumented invokes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_event, make_inspection, make_run_record
from typer.testing import CliRunner

from argus.check import evaluate_run, is_run_clean
from argus.cli.main import app
from argus.storage import save_run

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _clear_argus_run_id(monkeypatch):
    monkeypatch.delenv("ARGUS_RUN_ID", raising=False)


def _now(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


# ── evaluate_run ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_evaluate_run_clean_passes():
    record = make_run_record(events=[make_event()], status="clean", run_id="clean-1")
    result = evaluate_run(record)
    assert result.passed is True
    assert is_run_clean(record) is True
    assert result.failing_nodes == ()


@pytest.mark.unit
def test_evaluate_run_ignores_retried_inspection_failures():
    record = make_run_record(
        events=[
            make_event(
                node_name="retrieve",
                status="retried",
                inspection=make_inspection(
                    missing=["documents"],
                    is_silent=True,
                    severity="critical",
                    message="missing documents",
                ),
            ),
            make_event(node_name="retrieve", status="pass"),
        ],
        status="clean",
        run_id="retry-resolved",
    )
    result = evaluate_run(record)
    assert result.passed is True
    assert result.failing_nodes == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    ["crashed", "silent_failure", "semantic_fail"],
)
def test_evaluate_run_unclean_overall_fails(status):
    record = make_run_record(
        events=[make_event(status="fail" if status != "crashed" else "crashed")],
        status=status,
        run_id=f"{status}-1",
    )
    result = evaluate_run(record)
    assert result.passed is False
    assert status in result.summary


@pytest.mark.unit
def test_evaluate_run_node_semantic_fail_fails_even_if_overall_clean():
    record = make_run_record(
        events=[make_event(node_name="judge", status="semantic_fail")],
        status="clean",
        run_id="sem-node",
    )
    result = evaluate_run(record)
    assert result.passed is False
    assert "judge" in result.failing_nodes


@pytest.mark.unit
def test_evaluate_run_missing_fields_fails():
    record = make_run_record(
        events=[
            make_event(
                node_name="retrieve",
                status="pass",
                inspection=make_inspection(
                    missing=["documents"],
                    is_silent=True,
                    severity="critical",
                    message="missing documents",
                ),
            )
        ],
        status="clean",
        run_id="missing-fields",
    )
    result = evaluate_run(record)
    assert result.passed is False
    assert "retrieve" in result.failing_nodes
    assert any("missing_fields" in r for r in result.reasons)


@pytest.mark.unit
def test_evaluate_run_tool_failure_fails():
    from argus.models import ToolFailure

    record = make_run_record(
        events=[
            make_event(
                node_name="api_call",
                status="fail",
                inspection=make_inspection(
                    is_silent=False,
                    has_tool_failure=True,
                    tool_failures=[
                        ToolFailure(
                            failure_type="error_response",
                            field_name="error",
                            severity="critical",
                            evidence="status 503",
                        )
                    ],
                    severity="critical",
                    message="tool failure",
                ),
            )
        ],
        status="silent_failure",
        run_id="tool-fail",
    )
    result = evaluate_run(record)
    assert result.passed is False
    assert "api_call" in result.failing_nodes


# ── argus check CLI ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_check_last_clean_exits_zero():
    record = make_run_record(events=[make_event()], status="clean", run_id="cli-clean")
    record.started_at = _now()
    save_run(record)
    result = CliRunner().invoke(app, ["check", "last"])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output
    assert "pass" in result.output


@pytest.mark.unit
def test_check_no_args_is_last():
    record = make_run_record(events=[make_event()], status="clean", run_id="cli-default")
    record.started_at = _now()
    save_run(record)
    result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 0, result.output


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    ["crashed", "silent_failure", "semantic_fail"],
)
def test_check_last_unclean_exits_one(status):
    node_status = "crashed" if status == "crashed" else "fail"
    record = make_run_record(
        events=[make_event(status=node_status)],
        status=status,
        run_id=f"cli-{status}",
    )
    record.started_at = _now()
    save_run(record)
    result = CliRunner().invoke(app, ["check", "last"])
    assert result.exit_code == 1, result.output
    assert "fail" in result.output


@pytest.mark.unit
def test_check_specific_run_id_ignores_newer_failure():
    clean = make_run_record(events=[make_event()], status="clean", run_id="keep-clean")
    clean.started_at = _now(-10)
    crashed = make_run_record(
        events=[make_event(status="crashed")],
        status="crashed",
        run_id="newer-crash",
    )
    crashed.started_at = _now()
    save_run(clean)
    save_run(crashed)

    last = CliRunner().invoke(app, ["check", "last"])
    assert last.exit_code == 1

    named = CliRunner().invoke(app, ["check", "keep-clean"])
    assert named.exit_code == 0, named.output

    prefix = CliRunner().invoke(app, ["check", "keep-cle"])
    assert prefix.exit_code == 0, prefix.output


@pytest.mark.unit
def test_check_argus_run_id_wins_over_last(monkeypatch):
    selected = make_run_record(events=[make_event()], status="clean", run_id="env-clean")
    selected.started_at = _now(-10)
    newer = make_run_record(
        events=[make_event(status="crashed")],
        status="crashed",
        run_id="newer-crash",
    )
    newer.started_at = _now()
    selected_path = save_run(selected)
    save_run(newer)
    monkeypatch.setenv("ARGUS_RUN_ID", selected.run_id)

    result = CliRunner().invoke(app, ["check", "last"])

    assert result.exit_code == 0, result.output
    assert f"checked  {selected_path}" in result.output


@pytest.mark.unit
def test_check_explicit_id_wins_over_argus_run_id(monkeypatch):
    selected = make_run_record(events=[make_event()], status="clean", run_id="cli-clean")
    env_run = make_run_record(
        events=[make_event(status="crashed")],
        status="crashed",
        run_id="env-crash",
    )
    selected_path = save_run(selected)
    save_run(env_run)
    monkeypatch.setenv("ARGUS_RUN_ID", env_run.run_id)

    result = CliRunner().invoke(app, ["check", selected.run_id])

    assert result.exit_code == 0, result.output
    assert f"checked  {selected_path}" in result.output


@pytest.mark.unit
def test_check_missing_argus_run_id_exits_one(monkeypatch):
    monkeypatch.setenv("ARGUS_RUN_ID", "missing-env-run")

    result = CliRunner().invoke(app, ["check", "last"])

    assert result.exit_code == 1
    assert "missing-env-run" in result.output
    assert "No run found" in result.output


@pytest.mark.unit
def test_check_no_runs_exits_one():
    result = CliRunner().invoke(app, ["check", "last"])
    assert result.exit_code == 1
    assert "No runs found" in result.output


@pytest.mark.unit
def test_check_missing_run_id_exits_one():
    result = CliRunner().invoke(app, ["check", "does-not-exist"])
    assert result.exit_code == 1
    assert "Error" in result.output


@pytest.mark.unit
def test_check_help_mentions_ci_gate():
    result = CliRunner().invoke(app, ["check", "--help"])
    assert result.exit_code == 0
    assert "not clean" in result.output.lower() or "last" in result.output.lower()


# ── pytest --argus (already-instrumented invoke) ─────────────────────────────

_CLEAN_TEST = """
from typing import TypedDict
from langgraph.graph import END, StateGraph
from argus import ArgusWatcher

class S(TypedDict):
    n: int

def test_clean_invoke():
    g = StateGraph(S)
    g.add_node("inc", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("inc")
    g.add_edge("inc", END)
    app = ArgusWatcher(semantic_judge=False, investigate=False, record_http=False).attach(g)
    assert app.invoke({"n": 0})["n"] == 1
"""

_SILENT_TEST = """
from typing import TypedDict
from langgraph.graph import END, StateGraph
from argus import ArgusWatcher

class S(TypedDict, total=False):
    n: int
    results: list
    error: str
    status_code: int

def test_silent_invoke():
    def api_call(state):
        return {"results": [], "error": "Connection refused", "status_code": 503}

    def process(state):
        return {"n": 1}

    g = StateGraph(S)
    g.add_node("api_call", api_call)
    g.add_node("process", process)
    g.set_entry_point("api_call")
    g.add_edge("api_call", "process")
    g.add_edge("process", END)
    app = ArgusWatcher(semantic_judge=False, investigate=False, record_http=False).attach(g)
    app.invoke({"n": 0})
"""


def _prepare_plugin_project(pytester: pytest.Pytester) -> None:
    pytester.makefile(".toml", pyproject="[project]\nname = 'demo'\n")
    (pytester.path / ".argus" / "runs").mkdir(parents=True, exist_ok=True)


@pytest.mark.unit
def test_pytest_argus_clean_instrumented_invoke_passes(pytester: pytest.Pytester):
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_CLEAN_TEST)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(passed=1)


@pytest.mark.unit
def test_pytest_argus_silent_failure_fails_the_test(pytester: pytest.Pytester):
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_TEST)
    result = pytester.runpytest("--argus", "-q")
    result.assert_outcomes(failed=1)
    assert "argus check failed" in str(result.stdout) + str(result.stderr)


@pytest.mark.unit
def test_pytest_without_argus_flag_does_not_fail_silent_invoke(pytester: pytest.Pytester):
    _prepare_plugin_project(pytester)
    pytester.makepyfile(_SILENT_TEST)
    result = pytester.runpytest("-q")
    result.assert_outcomes(passed=1)


# ── argus check --format json / --fail-on ────────────────────────────────────


def _unclean_record(run_id: str):
    record = make_run_record(
        events=[
            make_event(
                node_name="search",
                status="fail",
                inspection=make_inspection(
                    missing=["documents"], is_silent=True, severity="critical"
                ),
            )
        ],
        status="silent_failure",
        run_id=run_id,
    )
    from argus.findings import collect_findings

    record.findings = collect_findings(record.steps)
    record.started_at = _now()
    return record


@pytest.mark.unit
def test_check_json_fail_shape_and_exit_code():
    import json

    save_run(_unclean_record("json-fail"))
    result = CliRunner().invoke(app, ["check", "last", "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)  # stdout is only the JSON object
    assert payload["run_id"] == "json-fail"
    assert payload["overall_status"] == "silent_failure"
    assert payload["passed"] is False
    assert payload["fail_on"] is None
    assert "search" in payload["failing_nodes"]
    assert payload["findings"] and payload["findings"][0]["type"] == "missing_field"
    for key in ("id", "node", "type", "severity", "reason", "source"):
        assert key in payload["findings"][0]


@pytest.mark.unit
def test_check_json_clean_exits_zero():
    import json

    record = make_run_record(events=[make_event()], status="clean", run_id="json-clean")
    record.started_at = _now()
    save_run(record)
    result = CliRunner().invoke(app, ["check", "last", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["passed"] is True and payload["findings"] == []


@pytest.mark.unit
def test_check_fail_on_filters_gate():
    save_run(_unclean_record("failon-1"))
    # silent_failure run; only crashes should fail → passes
    ok = CliRunner().invoke(app, ["check", "last", "--fail-on", "crashed"])
    assert ok.exit_code == 0, ok.output
    # include silent_failure → fails
    bad = CliRunner().invoke(app, ["check", "last", "--fail-on", "crashed,silent_failure"])
    assert bad.exit_code == 1


@pytest.mark.unit
def test_check_fail_on_rejects_unknown_value():
    import json

    save_run(_unclean_record("failon-2"))
    result = CliRunner().invoke(app, ["check", "last", "--fail-on", "bogus", "--format", "json"])
    assert result.exit_code == 2
    assert "bogus" in json.loads(result.output)["error"]
