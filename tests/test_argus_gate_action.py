"""The GitHub Action (`.github/actions/argus-gate/action.yml`): its inputs and its gate step.

The gate step is a bash script inside the YAML. These tests take that script out of the file and
run it the way the runner does (`bash -eo pipefail`) with stand-ins for `python` and `argus` that
write down how they were called, so what the action does is checked rather than what it says.

Cloud mode hands the verdict to a team workspace: tests run with `--argus-report-only`, telemetry
is off for them, and `argus cloud upload --final` follows. The job fails only if a test fails for
its own reasons or the upload fails. Everything else, `cloud: "false"` above all, must do exactly
what it did before the input existed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).parents[1] / ".github/actions/argus-gate/action.yml"
SPEC = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
GATE = next(step for step in SPEC["runs"]["steps"] if step.get("id") == "gate")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# One stand-in for both `python` and `argus`: log the call, say what the environment held,
# and exit with the code the test asked for.
_STUB = """#!/bin/bash
name=$(basename "$0")
echo "$name $*" >> "$FAKE_LOG"
case "$name $*" in
  "python -c "*) echo "$FAKE_RUNS"; exit 0 ;;
  "python -m pytest"*)
    echo "env TELEMETRY=${ARGUS_TELEMETRY-unset} CLOUD_URL=${ARGUS_CLOUD_URL-unset}" \
      "RUN_ID=${ARGUS_RUN_ID-unset}" >> "$FAKE_LOG"
    for id in $FAKE_NEW_RUNS; do : > "$FAKE_RUNS/$id.json"; done
    exit "${FAKE_PYTEST_RC:-0}" ;;
  "argus cloud upload"*) exit "${FAKE_UPLOAD_RC:-0}" ;;
  "argus cloud pull-baselines"*) exit "${FAKE_PULL_RC:-0}" ;;
  "argus check"*) exit "${FAKE_CHECK_RC:-0}" ;;
esac
exit 0
"""


def _run_gate(tmp_path, monkeypatch, **inputs):
    """Run the gate step's script: (exit code, calls made, GITHUB_OUTPUT as a dict, output)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("python", "argus"):
        stub = bin_dir / name
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    runs = tmp_path / "runs"
    runs.mkdir()
    log = tmp_path / "calls.log"
    github_output = tmp_path / "github_output"
    github_output.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_ACTION_PATH": "/action",
        "GITHUB_JOB": "tests",
        "GITHUB_RUN_ATTEMPT": "1",
        "FAKE_LOG": str(log),
        "FAKE_RUNS": str(runs),
        "INPUT_RUN_ID": "",
        "INPUT_PYTEST_ARGS": "tests/ -x",
        "INPUT_STRICT": "critical_only",
        "INPUT_CLOUD": "false",
        "INPUT_CLOUD_URL": "",
    }
    env.update(
        {
            k if k.startswith(("FAKE_", "ARGUS_")) else f"INPUT_{k.upper()}": v
            for k, v in inputs.items()
        }
    )
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", GATE["run"]],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return proc.returncode, calls, outputs, proc.stdout + proc.stderr


def _commands(calls):
    return [c for c in calls if not c.startswith("env ")]


# ── the inputs ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cloud_defaults_to_off_and_cloud_url_to_empty():
    inputs = SPEC["inputs"]
    assert inputs["cloud"]["default"] == "false" and inputs["cloud"]["required"] is False
    assert inputs["cloud-url"]["default"] == "" and inputs["cloud-url"]["required"] is False


@pytest.mark.unit
def test_the_input_docs_show_the_permission_the_job_needs():
    for name in ("cloud", "cloud-url"):
        text = " ".join(SPEC["inputs"][name]["description"].split())
        assert "id-token: write" in text
    assert "cloud-url" in SPEC["inputs"]["cloud"]["description"]
    assert "Required when" in SPEC["inputs"]["cloud-url"]["description"]


@pytest.mark.unit
def test_the_gate_step_gets_both_inputs_through_its_environment_not_its_script():
    env = GATE["env"]
    assert env["INPUT_CLOUD"] == "${{ inputs.cloud }}"
    assert env["INPUT_CLOUD_URL"] == "${{ inputs.cloud-url }}"
    assert "${{" not in GATE["run"]  # an input is never pasted into the shell


@pytest.mark.unit
def test_the_other_steps_are_untouched():
    steps = SPEC["runs"]["steps"]
    assert [s.get("id") or s["name"] for s in steps] == [
        "Install argus-agents",
        "gate",
        "Upload ARGUS runs",
        "Fail if ARGUS failed",
    ]
    assert steps[2]["if"] == "steps.gate.outputs.rc != '0' && inputs.upload-runs == 'true'"
    assert steps[3]["if"] == "steps.gate.outputs.rc != '0'"


# ── cloud: "false" is what it always was ─────────────────────────────────────


@pytest.mark.unit
def test_without_cloud_it_runs_pytest_with_argus_and_nothing_else(tmp_path, monkeypatch):
    code, calls, outputs, _ = _run_gate(tmp_path, monkeypatch, FAKE_NEW_RUNS="r1 r2")
    assert code == 0 and outputs["rc"] == "0"
    assert _commands(calls) == [
        "python -c from argus.storage import runs_dir; print(runs_dir())",
        "python -m pytest --argus tests/ -x",
        "python /action/summary.py r1 r2",
    ]
    assert (
        calls[2] == "env TELEMETRY=unset CLOUD_URL=unset RUN_ID=unset"
    )  # no cloud, no telemetry switch
    assert set(outputs) == {"rc", "bundle", "artifact"}


@pytest.mark.unit
@pytest.mark.parametrize("rc", [1, 2, 5])
def test_without_cloud_a_failing_test_run_fails_the_gate_with_its_code(tmp_path, monkeypatch, rc):
    code, calls, outputs, _ = _run_gate(tmp_path, monkeypatch, FAKE_PYTEST_RC=str(rc))
    assert code == 0 and outputs["rc"] == str(rc)  # the later steps exit with it
    assert not any("cloud" in c for c in _commands(calls))


@pytest.mark.unit
def test_without_cloud_a_run_id_is_graded_with_argus_check(tmp_path, monkeypatch):
    code, calls, outputs, _ = _run_gate(
        tmp_path,
        monkeypatch,
        run_id="20261019-101010-abc123",
        strict="warn_as_fail",
        FAKE_CHECK_RC="1",
    )
    assert code == 0 and outputs["rc"] == "1"
    assert "argus check 20261019-101010-abc123 --strict warn_as_fail" in calls
    assert not any("pytest" in c or "cloud" in c for c in _commands(calls))


@pytest.mark.unit
def test_without_cloud_the_environments_run_id_still_selects_a_run(tmp_path, monkeypatch):
    code, calls, outputs, _ = _run_gate(tmp_path, monkeypatch, ARGUS_RUN_ID="from-env")
    assert "argus check from-env --strict critical_only" in calls


@pytest.mark.unit
def test_a_cloud_url_is_ignored_while_cloud_is_off(tmp_path, monkeypatch):
    code, calls, outputs, _ = _run_gate(
        tmp_path, monkeypatch, cloud_url="https://workspace.example.com"
    )
    assert "python -m pytest --argus tests/ -x" in calls and not any("upload" in c for c in calls)
    assert calls[2] == "env TELEMETRY=unset CLOUD_URL=unset RUN_ID=unset"


# ── cloud: "true" ────────────────────────────────────────────────────────────

CLOUD = {"cloud": "true", "cloud_url": "https://workspace.example.com"}


@pytest.mark.unit
def test_cloud_runs_report_only_pytest_with_telemetry_off_then_uploads_the_final_cards(
    tmp_path, monkeypatch
):
    code, calls, outputs, _ = _run_gate(tmp_path, monkeypatch, FAKE_NEW_RUNS="r1", **CLOUD)
    assert code == 0 and outputs["rc"] == "0"
    assert _commands(calls) == [
        "python -c from argus.storage import runs_dir; print(runs_dir())",
        "argus cloud pull-baselines",  # what main learned, before anything is graded
        "python -m pytest --argus-report-only tests/ -x",
        "argus cloud upload --final",
        "python /action/summary.py r1",  # the job summary is still written
    ]
    pytest_env = next(c for c in calls if c.startswith("env "))
    assert pytest_env == "env TELEMETRY=0 CLOUD_URL=https://workspace.example.com RUN_ID=unset"


@pytest.mark.unit
def test_cloud_never_runs_pytest_with_the_flag_that_fails_tests(tmp_path, monkeypatch):
    _, calls, _, _ = _run_gate(tmp_path, monkeypatch, **CLOUD)
    assert not any(c.split()[:4] == ["python", "-m", "pytest", "--argus"] for c in calls)


@pytest.mark.unit
def test_cloud_fails_for_the_tests_own_reasons_and_still_uploads(tmp_path, monkeypatch):
    code, calls, outputs, _ = _run_gate(tmp_path, monkeypatch, FAKE_PYTEST_RC="1", **CLOUD)
    assert outputs["rc"] == "1"
    assert "argus cloud upload --final" in calls  # the workspace must hear the job ended


@pytest.mark.unit
def test_cloud_fails_when_the_upload_does(tmp_path, monkeypatch):
    code, calls, outputs, output = _run_gate(tmp_path, monkeypatch, FAKE_UPLOAD_RC="1", **CLOUD)
    assert outputs["rc"] == "1"
    assert "::error::" in output and "team workspace" in output


@pytest.mark.unit
def test_cloud_fails_when_the_baselines_cannot_be_pulled_but_still_tests_and_uploads(
    tmp_path, monkeypatch
):
    # Graded without its baseline, a regression would pass unseen: that is not a green job.
    code, calls, outputs, output = _run_gate(tmp_path, monkeypatch, FAKE_PULL_RC="1", **CLOUD)
    assert outputs["rc"] == "1"
    assert "::error::" in output and "baselines" in output
    assert "python -m pytest --argus-report-only tests/ -x" in calls
    assert "argus cloud upload --final" in calls


@pytest.mark.unit
def test_cloud_keeps_the_test_runs_exit_code_when_the_pull_fails_too(tmp_path, monkeypatch):
    _, _, outputs, _ = _run_gate(
        tmp_path, monkeypatch, FAKE_PULL_RC="1", FAKE_PYTEST_RC="5", **CLOUD
    )
    assert outputs["rc"] == "5"


@pytest.mark.unit
def test_without_cloud_no_baselines_are_pulled(tmp_path, monkeypatch):
    _, calls, _, _ = _run_gate(tmp_path, monkeypatch)
    assert not any("pull-baselines" in c for c in calls)


@pytest.mark.unit
def test_cloud_keeps_the_test_runs_exit_code_when_both_fail(tmp_path, monkeypatch):
    _, _, outputs, _ = _run_gate(
        tmp_path, monkeypatch, FAKE_PYTEST_RC="5", FAKE_UPLOAD_RC="1", **CLOUD
    )
    assert outputs["rc"] == "5"


@pytest.mark.unit
def test_cloud_passes_when_tests_and_upload_do_whatever_the_runs_looked_like(
    tmp_path, monkeypatch
):
    # Runs that were not clean never reach the exit code: the workspace's policy decides.
    _, _, outputs, _ = _run_gate(
        tmp_path, monkeypatch, FAKE_NEW_RUNS="bad-run", FAKE_CHECK_RC="1", **CLOUD
    )
    assert outputs["rc"] == "0"


@pytest.mark.unit
def test_cloud_needs_a_workspace_url_and_says_so_before_running_anything(tmp_path, monkeypatch):
    code, calls, outputs, output = _run_gate(tmp_path, monkeypatch, cloud="true", cloud_url="")
    assert code == 2 and "::error::cloud-url is required" in output
    assert calls == [] and outputs == {}


@pytest.mark.unit
def test_cloud_cannot_be_combined_with_run_id(tmp_path, monkeypatch):
    code, calls, outputs, output = _run_gate(tmp_path, monkeypatch, run_id="r1", **CLOUD)
    assert code == 2 and "run-id" in output and "::error::" in output
    assert calls == [] and outputs == {}


@pytest.mark.unit
def test_cloud_grades_the_pytest_session_even_if_a_run_id_is_left_in_the_environment(
    tmp_path, monkeypatch
):
    _, calls, _, _ = _run_gate(tmp_path, monkeypatch, ARGUS_RUN_ID="left-over", **CLOUD)
    assert "python -m pytest --argus-report-only tests/ -x" in calls
    assert not any(c.startswith("argus check") for c in calls)
    assert next(c for c in calls if c.startswith("env ")).endswith("RUN_ID=unset")


@pytest.mark.unit
def test_the_documentation_shows_the_permission_and_the_inputs():
    readme = (ACTION.parents[3] / "README.md").read_text(encoding="utf-8")
    assert "id-token: write" in readme and 'cloud: "true"' in readme and "cloud-url:" in readme


@pytest.mark.unit
def test_the_data_page_says_how_the_action_keeps_an_uploaded_run_out_of_telemetry():
    page = " ".join((ACTION.parents[3] / "docs/DATA.md").read_text(encoding="utf-8").split())
    assert "is not also sent as telemetry" in page
    assert 'with `cloud: "true"` it exports `ARGUS_TELEMETRY=0` before it runs your tests' in page
    assert "#a-run-sent-to-a-workspace-is-not-also-telemetry" in page  # linked from Paying teams
