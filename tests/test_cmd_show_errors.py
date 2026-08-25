"""``argus show <id>`` must fail loudly when the id can't be resolved.

Companion to the #33 family: positional ids now resolve correctly, but the
error paths still exited 0 (unknown id) or dumped a raw traceback (ambiguous
prefix) — either way a script can't tell success from failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import make_event, make_run_record
from typer.testing import CliRunner

from argus.cli.main import app
from argus.storage import save_run


def _now(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def _save(run_id: str, offset_s: float = 0.0):
    record = make_run_record(events=[make_event()], status="clean", run_id=run_id)
    record.started_at = _now(offset_s)
    save_run(record)
    return record


# ── happy paths keep working ─────────────────────────────────────────────────


def test_show_known_full_id_renders_that_run():
    _save("20260101-000001-aaaa1111")
    newer = _save("20260101-000002-bbbb2222", offset_s=5)
    result = CliRunner().invoke(app, ["show", "20260101-000001-aaaa1111"])
    assert result.exit_code == 0, result.output
    assert "20260101-000001-aaaa1111" in result.output
    assert newer.run_id not in result.output.split("argus")[1]  # header shows requested run


def test_show_unique_prefix_resolves():
    _save("20260101-000003-cccc3333")
    result = CliRunner().invoke(app, ["show", "20260101-000003"])
    assert result.exit_code == 0, result.output
    assert "20260101-000003-cccc3333" in result.output


def test_show_run_form_resolves_requested_id_not_last():
    _save("20260101-000004-dddd4444")
    _save("20260101-000005-eeee5555", offset_s=5)
    result = CliRunner().invoke(app, ["show", "run", "20260101-000004-dddd4444"])
    assert result.exit_code == 0, result.output
    assert "20260101-000004-dddd4444" in result.output


# ── unresolvable ids exit nonzero with a clean message ──────────────────────


def test_show_unknown_id_exits_one_with_error():
    _save("20260101-000006-ffff6666")
    result = CliRunner().invoke(app, ["show", "20990101-999999-zzzz9999"])
    assert result.exit_code == 1, result.output
    assert "Error" in result.output


def test_show_ambiguous_prefix_exits_one_without_traceback():
    _save("dupcase-aaa1")
    _save("dupcase-aaa2")
    result = CliRunner().invoke(app, ["show", "dupcase-aaa"])
    assert result.exit_code == 1, result.output
    assert "mbiguous" in result.output
    assert "Traceback" not in result.output


def test_show_json_unknown_id_exits_one():
    result = CliRunner().invoke(app, ["show", "no-such-run", "--json"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_show_json_ambiguous_prefix_exits_one_without_traceback():
    _save("dupjson-aaa1")
    _save("dupjson-aaa2")
    result = CliRunner().invoke(app, ["show", "dupjson-aaa", "--json"])
    assert result.exit_code == 1, result.output
    assert "mbiguous" in result.output
    assert "Traceback" not in result.output
