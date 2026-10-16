"""Telemetry sender: switches, notice, outbox, cap, dedupe, and the save_run hook."""

from __future__ import annotations

import json

import pytest
from conftest import make_event, make_run_record

from argus import storage
from argus.models import Finding
from argus.telemetry import send

URL = "https://intake.test/telemetry"


@pytest.fixture
def posts(monkeypatch, tmp_path):
    """Telemetry on, endpoint set, settings in tmp, network captured."""
    monkeypatch.delenv("ARGUS_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setattr(send, "SETTINGS_FILE", tmp_path / "home" / "telemetry.json")
    monkeypatch.setattr(send, "ENDPOINT", URL)
    sent: list = []
    monkeypatch.setattr(send, "_post", lambda url, card: sent.append((url, card)))
    monkeypatch.setattr(send, "_start", lambda fn, *args: fn(*args))  # no thread in tests
    return sent


def _record(run_id="r1", status="fail", node="a"):
    record = make_run_record(events=[make_event(node_name=node)], run_id=run_id, status=status)
    record.findings = [
        Finding(
            id="x",
            node=node,
            type="empty_result",
            severity="critical",
            reason="r",
            source="heuristic",
        )
    ]
    return record


# ── switches ────────────────────────────────────────────────────────────────


def test_on_by_default(posts):
    assert send.status() == (True, "default")
    assert send.send_run(_record()) is not None
    assert len(posts) == 1 and posts[0][0] == URL


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_argus_telemetry_env_turns_it_off(posts, monkeypatch, value):
    monkeypatch.setenv("ARGUS_TELEMETRY", value)
    assert send.status() == (False, "ARGUS_TELEMETRY")
    assert send.send_run(_record()) is None
    assert posts == []


def test_do_not_track_wins_over_everything(posts, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("ARGUS_TELEMETRY", "1")
    assert send.status() == (False, "DO_NOT_TRACK")
    assert send.send_run(_record()) is None


def test_saved_off_and_env_override(posts, monkeypatch):
    send.set_enabled(False)
    assert send.status() == (False, "argus telemetry off")
    assert send.send_run(_record()) is None
    monkeypatch.setenv("ARGUS_TELEMETRY", "1")
    assert send.status() == (True, "ARGUS_TELEMETRY")
    send.set_enabled(True)
    monkeypatch.delenv("ARGUS_TELEMETRY")
    assert send.status() == (True, "argus telemetry on")


def test_no_endpoint_means_inert(posts, monkeypatch):
    monkeypatch.setattr(send, "ENDPOINT", None)
    assert send.send_run(_record()) is None
    assert posts == []
    assert not send.outbox_dir().exists()


# ── identity ────────────────────────────────────────────────────────────────


def test_install_id_and_secret_persist_and_secret_is_never_sent(posts):
    send.send_run(_record("r1"))
    send.send_run(_record("r2", node="b"))
    saved = json.loads(send.SETTINGS_FILE.read_text())
    assert saved["install_id"].startswith("i_") and len(saved["secret"]) >= 32
    assert posts[0][1]["install"] == posts[1][1]["install"] == saved["install_id"]
    assert saved["secret"] not in json.dumps([card for _, card in posts])
    assert oct(send.SETTINGS_FILE.stat().st_mode)[-3:] == "600"


# ── notice ──────────────────────────────────────────────────────────────────


def test_notice_printed_once(posts, capsys):
    send.send_run(_record("r1"))
    first = capsys.readouterr().err
    send.send_run(_record("r2", node="b"))
    assert "argus telemetry off" in first and "DATA.md" in first
    assert capsys.readouterr().err == ""


# ── outbox, cap, dedupe ─────────────────────────────────────────────────────


def test_every_sent_card_is_in_the_outbox(posts):
    path = send.send_run(_record())
    assert path is not None and path.parent == send.outbox_dir()
    assert json.loads(path.read_text()) == posts[0][1]


def test_identical_runs_send_once_per_day(posts):
    assert send.send_run(_record("r1")) is not None
    assert send.send_run(_record("r2")) is None  # same graph, verdict, findings
    assert send.send_run(_record("r3", status="clean")) is not None
    assert len(posts) == 2


def test_daily_cap(posts, monkeypatch):
    monkeypatch.setattr(send, "DAILY_CAP", 2)
    for i in range(4):
        send.send_run(_record(f"r{i}", node=f"n{i}"))
    assert len(posts) == 2


def test_cap_resets_on_a_new_day(posts, monkeypatch):
    monkeypatch.setattr(send, "DAILY_CAP", 1)
    send.send_run(_record("r1", node="a"))
    monkeypatch.setattr(send, "_today", lambda: "2099-01-01")
    send.send_run(_record("r2", node="b"))
    assert len(posts) == 2


def test_outbox_is_pruned(posts, monkeypatch):
    monkeypatch.setattr(send, "OUTBOX_KEEP", 3)
    for i in range(5):
        send.send_run(_record(f"r{i}", node=f"n{i}"))
    assert len(list(send.outbox_dir().glob("*.json"))) == 3


def test_ci_detected(posts, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    send.send_run(_record())
    assert posts[0][1]["ci"] is True


# ── never breaks a run ──────────────────────────────────────────────────────


def test_network_error_is_swallowed(monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setattr(send, "SETTINGS_FILE", tmp_path / "telemetry.json")
    send._post("http://127.0.0.1:9/unreachable", {"schema": 1})  # returns, no raise


def test_save_run_sends_a_card(posts, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "_runs_path", lambda: tmp_path)
    storage.save_run(_record("saved"))
    assert len(posts) == 1


def test_save_run_survives_a_broken_sender(posts, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "_runs_path", lambda: tmp_path)
    monkeypatch.setattr(send, "build_card", lambda *a, **k: 1 / 0)
    assert storage.save_run(_record("saved")).exists()


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_off_on_status_show_last(posts):
    from typer.testing import CliRunner

    from argus.cli.main import app

    runner = CliRunner()
    assert "on" in runner.invoke(app, ["telemetry", "status"]).output
    assert runner.invoke(app, ["telemetry", "off"]).exit_code == 0
    assert send.status() == (False, "argus telemetry off")
    assert runner.invoke(app, ["telemetry", "on"]).exit_code == 0
    assert "No card" in runner.invoke(app, ["telemetry", "show-last"]).output
    send.send_run(_record())
    shown = runner.invoke(app, ["telemetry", "show-last"]).output
    assert '"fingerprint"' in shown
