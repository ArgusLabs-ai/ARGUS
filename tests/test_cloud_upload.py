"""``argus cloud upload [--final]``: send this job's run cards to a team workspace.

Everything runs against ``tests/fake_workspace.py``, a stdlib ``http.server`` on 127.0.0.1 that
plays both GitHub's OIDC endpoint and the workspace. The point of the command is that it fails
loudly: any non-2xx answer, timeout or missing precondition is exit 1 and one line, and neither
the token nor a card ever appears in what it prints.
"""

from __future__ import annotations

import socket

import pytest
from conftest import make_event, make_run_record
from fake_workspace import OIDC_TOKEN, REQUEST_TOKEN, FakeWorkspace, point_at, serve
from typer.testing import CliRunner

from argus import cloud_upload, run_context, storage
from argus.cli.main import app
from argus.cloud_card import build_card

SECRETS = (OIDC_TOKEN, REQUEST_TOKEN, "SERVER-BODY-SECRET")


@pytest.fixture
def workspace(monkeypatch):
    fake = FakeWorkspace()
    with serve(fake):
        point_at(monkeypatch, fake)
        yield fake


def _failing(run_id="20261019-101010-b2"):
    event = make_event(node_name="fetch", status="fail", output={"docs": []})
    return make_run_record(events=[event], status="silent_failure", run_id=run_id)


def _clean(run_id="20261019-101010-a1"):
    return make_run_record(
        events=[make_event(node_name="fetch", output={"docs": ["a"]})], run_id=run_id
    )


def _session(*records, listed=True):
    """Save ``records`` the way a pytest session does, and list them."""
    run_context.begin_session(storage.session_path(), fresh=True)
    try:
        for record in records:
            storage.save_run(record)
    finally:
        run_context.end_session()
    if not listed:
        storage.session_path().unlink()


def _upload(*args):
    return CliRunner().invoke(app, ["cloud", "upload", *args])


def _one_line(output: str) -> str:
    lines = [ln for ln in output.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    return lines[0]


def _prints_no_secret(output: str) -> None:
    for secret in SECRETS:
        assert secret not in output


# ── a good upload ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_it_posts_this_jobs_cards_with_the_oidc_token(workspace):
    first, second = _clean(), _failing()
    _session(first, second)
    result = _upload("--final")
    assert result.exit_code == 0, result.output
    assert _one_line(result.output) == "argus: uploaded 2 run cards (final)"

    oidc, post = workspace.requests
    assert oidc["method"] == "GET" and oidc["path"] == "/oidc?api-version=2.0&audience=argus-cloud"
    assert oidc["headers"]["authorization"] == f"bearer {REQUEST_TOKEN}"
    assert post["method"] == "POST" and post["path"] == "/api/v1/runs"
    assert post["headers"]["authorization"] == f"Bearer {OIDC_TOKEN}"
    assert post["headers"]["content-type"] == "application/json"

    body = workspace.uploaded()
    assert body == {"schema": 1, "final": True, "cards": [build_card(first), build_card(second)]}


@pytest.mark.unit
def test_the_body_names_no_repository_ref_or_commit(workspace):
    _session(_clean())
    assert _upload("--final").exit_code == 0
    assert set(workspace.uploaded()) == {"schema", "final", "cards"}  # the token says who
    assert "github" not in workspace.posts()[0]["body"].decode().lower()


@pytest.mark.unit
def test_without_final_the_upload_says_it_is_not_the_last(workspace):
    _session(_clean())
    result = _upload()
    assert result.exit_code == 0, result.output
    assert _one_line(result.output) == "argus: uploaded 1 run card"
    assert workspace.uploaded()["final"] is False


@pytest.mark.unit
def test_a_session_that_saved_no_runs_still_tells_the_workspace_it_is_over(workspace):
    _session()
    result = _upload("--final")
    assert result.exit_code == 0, result.output
    assert _one_line(result.output) == "argus: uploaded 0 run cards (final)"
    assert workspace.uploaded() == {"schema": 1, "final": True, "cards": []}


@pytest.mark.unit
def test_a_run_saved_twice_is_sent_once(workspace):
    record = _clean()
    _session(record, record)
    assert _upload("--final").exit_code == 0
    assert len(workspace.uploaded()["cards"]) == 1


@pytest.mark.unit
def test_only_the_runs_of_this_session_are_sent(workspace):
    stale = _failing(run_id="20250101-000000-stale0")
    storage.save_run(stale)  # a run file from an earlier job, in the same workspace
    _session(_clean())
    assert _upload("--final").exit_code == 0
    assert [c["run"]["run_id"] for c in workspace.uploaded()["cards"]] == ["20261019-101010-a1"]


# ── it fails loudly, in one line ─────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "reason"), [(401, "Unauthorized"), (500, "Internal Server Error")]
)
def test_a_non_2xx_answer_is_exit_1_naming_the_status(workspace, status, reason):
    _session(_failing())
    workspace.upload_status = status
    workspace.upload_body = b'{"error": "SERVER-BODY-SECRET"}'
    result = _upload("--final")
    assert result.exit_code == 1
    line = _one_line(result.output)
    assert line == f"argus: upload failed: HTTP {status} {reason} from 127.0.0.1"
    _prints_no_secret(result.output)


@pytest.mark.unit
@pytest.mark.parametrize("status", [199, 301, 400, 403, 404, 413, 429, 502, 503])
def test_every_other_status_outside_2xx_fails_too(workspace, status):
    _session(_clean())
    workspace.upload_status = status
    result = _upload("--final")
    assert result.exit_code == 1 and f"HTTP {status}" in result.output


@pytest.mark.unit
@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_any_2xx_is_a_success(workspace, status):
    _session(_clean())
    workspace.upload_status = status
    workspace.upload_body = b""
    assert _upload("--final").exit_code == 0


@pytest.mark.unit
def test_a_workspace_that_does_not_answer_in_time_is_exit_1(workspace, monkeypatch):
    _session(_failing())
    monkeypatch.setattr(cloud_upload, "UPLOAD_TIMEOUT_S", 0.3)
    workspace.upload_delay = 2.0
    result = _upload("--final")
    assert result.exit_code == 1
    assert _one_line(result.output) == "argus: upload failed: no answer from 127.0.0.1 in 0.3s"
    _prints_no_secret(result.output)


@pytest.mark.unit
def test_a_workspace_that_cannot_be_reached_is_exit_1(workspace, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("ARGUS_CLOUD_URL", f"http://127.0.0.1:{port}")  # nothing listens there
    _session(_failing())
    result = _upload("--final")
    assert result.exit_code == 1
    assert _one_line(result.output).startswith("argus: upload failed: could not reach 127.0.0.1")
    _prints_no_secret(result.output)


@pytest.mark.unit
def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(workspace):
    _session(_failing())
    workspace.redirect_to = f"{workspace.url}/elsewhere"
    result = _upload("--final")
    assert result.exit_code == 1 and "HTTP 307" in result.output
    assert [r["path"] for r in workspace.requests if r["method"] == "POST"] == ["/api/v1/runs"]
    assert not any(r["path"] == "/elsewhere" for r in workspace.requests)


@pytest.mark.unit
@pytest.mark.parametrize("gone", ["both", "url", "token"])
def test_outside_github_actions_it_is_exit_1_with_one_clear_line(workspace, monkeypatch, gone):
    _session(_failing())
    if gone in ("both", "url"):
        monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL")
    if gone in ("both", "token"):
        monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    result = _upload("--final")
    assert result.exit_code == 1
    line = _one_line(result.output)
    assert "GitHub Actions" in line and "id-token: write" in line
    assert workspace.requests == []  # nothing was asked of anyone


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "  "])
def test_a_blank_oidc_variable_counts_as_absent(workspace, monkeypatch, value):
    _session(_failing())
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", value)
    result = _upload("--final")
    assert result.exit_code == 1 and "id-token: write" in result.output
    assert workspace.requests == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, None, "HTTP 403"),
        (200, b"not json", "no token"),
        (200, b'{"count": 1}', "no token"),
        (200, b'{"value": ""}', "no token"),
        (200, b'{"value": 7}', "no token"),
        (200, b"[1]", "no token"),
    ],
)
def test_a_token_request_that_fails_stops_before_anything_is_sent(
    workspace, status, body, expected
):
    _session(_failing())
    workspace.oidc_status = status
    workspace.oidc_body = body if body is not None else b'{"message": "SERVER-BODY-SECRET"}'
    result = _upload("--final")
    assert result.exit_code == 1
    line = _one_line(result.output)
    assert "GitHub OIDC" in line and expected in line
    assert workspace.posts() == []
    _prints_no_secret(result.output)


@pytest.mark.unit
def test_a_missing_or_unusable_workspace_url_is_exit_1_before_any_request(workspace, monkeypatch):
    _session(_failing())
    for value in (
        None,
        "",
        "ftp://example.com",
        "http://example.com",
        "https://",
        "https://u:p@example.com",
    ):
        if value is None:
            monkeypatch.delenv("ARGUS_CLOUD_URL")
        else:
            monkeypatch.setenv("ARGUS_CLOUD_URL", value)
        result = _upload("--final")
        assert result.exit_code == 1, value
        assert "ARGUS_CLOUD_URL" in _one_line(result.output)
        assert ":p@" not in result.output
    assert workspace.requests == []


@pytest.mark.unit
def test_a_trailing_slash_on_the_workspace_url_is_fine(workspace, monkeypatch):
    _session(_clean())
    monkeypatch.setenv("ARGUS_CLOUD_URL", workspace.url + "/")
    assert _upload("--final").exit_code == 0
    assert workspace.posts()[0]["path"] == "/api/v1/runs"


@pytest.mark.unit
def test_no_session_list_is_exit_1_and_nothing_is_sent(workspace):
    _session(_clean(), listed=False)
    result = _upload("--final")
    assert result.exit_code == 1
    assert (
        "session_runs.txt" in _one_line(result.output) and "--argus-report-only" in result.output
    )
    assert workspace.requests == []


@pytest.mark.unit
def test_a_listed_run_that_cannot_be_read_stops_the_whole_upload(workspace):
    _session(_clean())
    storage.session_path().write_text(
        storage.session_path().read_text() + "20261019-000000-gone00\n", encoding="utf-8"
    )
    result = _upload("--final")
    assert result.exit_code == 1
    assert "20261019-000000-gone00" in _one_line(result.output)
    assert workspace.requests == []  # a partial upload would let the workspace judge half a job


@pytest.mark.unit
def test_a_card_that_cannot_be_built_names_the_run_and_the_error_class_only(
    workspace, monkeypatch
):
    _session(_clean())

    def boom(record, **_):
        raise ValueError("SERVER-BODY-SECRET is a value")

    monkeypatch.setattr(cloud_upload, "build_card", boom)
    result = _upload("--final")
    assert result.exit_code == 1
    assert "20261019-101010-a1" in result.output and "ValueError" in result.output
    _prints_no_secret(result.output)
    assert workspace.requests == []


@pytest.mark.unit
def test_nothing_about_the_command_is_printed_beyond_one_line(workspace):
    _session(_clean())
    result = _upload("--final")
    assert result.exit_code == 0
    _one_line(result.output)
    _prints_no_secret(result.output)


# ── the pieces ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_token_request_adds_the_audience_to_a_url_with_or_without_a_query():
    assert cloud_upload.audience_url("https://x/idtoken?api-version=2.0") == (
        "https://x/idtoken?api-version=2.0&audience=argus-cloud"
    )
    assert (
        cloud_upload.audience_url("https://x/idtoken") == "https://x/idtoken?audience=argus-cloud"
    )


@pytest.mark.unit
def test_the_cloud_command_group_has_upload_and_nothing_that_pulls():
    result = CliRunner().invoke(app, ["cloud", "--help"])
    assert result.exit_code == 0 and "upload" in result.output
    assert "pull" not in result.output


# ── a green run on the default branch sends its baseline part ────────────────


def _on_branch(monkeypatch, tmp_path, *, event="push", ref="refs/heads/main", default="main"):
    import json

    path = tmp_path / "event.json"
    path.write_text(json.dumps({"repository": {"default_branch": default}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_REF", ref)


@pytest.mark.unit
def test_a_clean_ci_run_pushed_to_the_default_branch_carries_a_baseline_part(
    workspace, monkeypatch, tmp_path
):
    record = _clean()
    record.environment = "ci"
    _session(record)
    _on_branch(monkeypatch, tmp_path)
    assert _upload("--final").exit_code == 0
    (card,) = workspace.uploaded()["cards"]
    assert card == build_card(record, baseline_part=True) and "baseline_part" in card


@pytest.mark.unit
@pytest.mark.parametrize(
    "kw",
    [
        {"event": "pull_request", "ref": "refs/pull/7/merge"},
        {"ref": "refs/heads/feature"},
        {"default": "trunk"},
    ],
)
def test_any_other_ref_sends_no_baseline_part(workspace, monkeypatch, tmp_path, kw):
    record = _clean()
    record.environment = "ci"
    _session(record)
    _on_branch(monkeypatch, tmp_path, **kw)
    assert _upload("--final").exit_code == 0
    assert "baseline_part" not in workspace.uploaded()["cards"][0]


@pytest.mark.unit
@pytest.mark.parametrize("event_file", ["{not json", "[]", '{"repository": null}', None])
def test_an_unreadable_event_file_means_not_the_default_branch(monkeypatch, tmp_path, event_file):
    _on_branch(monkeypatch, tmp_path)
    if event_file is None:
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing.json"))
    else:
        (tmp_path / "event.json").write_text(event_file)
    import os

    assert cloud_upload.on_default_branch(os.environ) is False
