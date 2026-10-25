"""``argus cloud pull-baselines``: the team's learned baselines, written for the recorder.

Against ``tests/fake_workspace.py``, as the upload tests are. The workspace answers
``{"schema": 1, "agents": {name: {"parts": [baseline, ...]}}}``; each agent's parts are merged
with ``trace_rules.merge_baselines`` into ``.argus/baselines/<agent>.json``. Nothing learned yet
(404, or an agent with no parts) is "learning", not an error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_workspace import OIDC_TOKEN, REQUEST_TOKEN, FakeWorkspace, point_at, serve
from typer.testing import CliRunner

from argus.cli.main import app
from argus.trace_rules import merge_baselines

pytestmark = pytest.mark.unit

A = {
    "version": 2,
    "nodes": {
        "fetch": {"keys": ["docs", "note"], "types": {}, "paths": {}, "lens": {"docs": [1, 3]}}
    },
}
B = {
    "version": 2,
    "nodes": {"fetch": {"keys": ["docs"], "types": {}, "paths": {}, "lens": {"docs": [2, 5]}}},
}


@pytest.fixture
def workspace(monkeypatch):
    fake = FakeWorkspace()
    with serve(fake):
        point_at(monkeypatch, fake)
        yield fake


def _answer(fake, agents, status=200):
    fake.baselines_status = status
    fake.baselines_body = json.dumps({"schema": 1, "agents": agents}).encode()


def _pull():
    return CliRunner().invoke(app, ["cloud", "pull-baselines"])


def _file(agent):
    path = Path(f".argus/baselines/{agent}.json")
    return json.loads(path.read_text()) if path.exists() else None


def test_each_agents_parts_are_merged_into_its_baseline_file(workspace):
    _answer(workspace, {"support-bot": {"parts": [A, B]}, "triage": {"parts": [B]}})
    result = _pull()
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "argus: pulled baselines for 2 agents"
    assert _file("support-bot") == merge_baselines([A, B])
    assert _file("triage") == merge_baselines([B])
    oidc, get = workspace.requests
    assert oidc["path"] == "/oidc?api-version=2.0&audience=argus-cloud"
    assert oidc["headers"]["authorization"] == f"bearer {REQUEST_TOKEN}"
    assert get["method"] == "GET" and get["headers"]["authorization"] == f"Bearer {OIDC_TOKEN}"


def test_nothing_learned_yet_is_learning_not_an_error(workspace):
    result = _pull()  # the fake answers 404
    assert result.exit_code == 0 and "learning" in result.output
    _answer(workspace, {"support-bot": {"parts": []}})
    result = _pull()
    assert result.exit_code == 0 and "1 learning" in result.output
    assert _file("support-bot") is None


@pytest.mark.parametrize(
    "entry",
    [
        {"parts": [A, {"nodes": []}]},
        {"parts": [{"nodes": {"fetch": {"lens": {"docs": [1]}}}}]},
        {"parts": "x"},
    ],
)
def test_an_agent_with_a_part_that_is_not_a_baseline_gets_no_file(workspace, entry):
    # Merging fewer parts would claim more than the runs agreed on: skip the agent whole.
    _answer(workspace, {"support-bot": entry, "triage": {"parts": [B]}})
    result = _pull()
    assert result.exit_code == 0 and "1 skipped" in result.output
    assert _file("support-bot") is None and _file("triage") == merge_baselines([B])


def test_an_agent_name_that_is_not_a_plain_file_name_writes_nothing(workspace):
    _answer(workspace, {"../../escape": {"parts": [A]}, "a/b": {"parts": [A]}})
    result = _pull()
    assert result.exit_code == 0 and "2 skipped" in result.output
    assert not Path("escape.json").exists() and not Path(".argus/a").exists()


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"schema": 2, "agents": {}}'])
def test_an_answer_that_is_not_a_baselines_list_is_exit_1(workspace, body):
    workspace.baselines_status, workspace.baselines_body = 200, body
    result = _pull()
    assert result.exit_code == 1 and "pull baselines failed" in result.output


def test_a_server_error_is_exit_1_and_prints_no_secret(workspace):
    _answer(workspace, {}, status=503)
    result = _pull()
    assert result.exit_code == 1 and "HTTP 503" in result.output
    assert OIDC_TOKEN not in result.output and REQUEST_TOKEN not in result.output


def test_outside_github_actions_it_is_exit_1(workspace, monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL")
    result = _pull()
    assert result.exit_code == 1 and "OIDC" in result.output and workspace.requests == []


def test_an_answer_nested_too_deep_to_parse_is_exit_1(workspace):
    workspace.baselines_status, workspace.baselines_body = 200, b"[" * 200_000
    result = _pull()
    assert result.exit_code == 1 and "pull baselines failed" in result.output


def test_a_planted_symlink_at_the_partial_file_is_not_followed(workspace, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched")
    Path(".argus/baselines").mkdir(parents=True)
    Path(".argus/baselines/triage.json.partial").symlink_to(target)
    _answer(workspace, {"triage": {"parts": [B]}})
    assert _pull().exit_code == 0
    assert target.read_text() == "untouched" and _file("triage") == merge_baselines([B])
