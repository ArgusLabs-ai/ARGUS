"""``argus key set/use`` must exit nonzero when the operation fails.

Both commands printed a friendly error ("Unknown provider", "No <p> key
set") and then exited 0, so scripts gating on the exit code silently
treated failed switches as success.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from argus.cli.main import app


@pytest.mark.unit
def test_key_use_unknown_provider_exits_one():
    result = CliRunner().invoke(app, ["key", "use", "bogus"])
    assert result.exit_code == 1, result.output
    assert "Unknown provider" in result.output


@pytest.mark.unit
def test_key_set_unknown_provider_exits_one():
    result = CliRunner().invoke(app, ["key", "set", "sk-test", "--provider", "bogus"])
    assert result.exit_code == 1, result.output
    assert "Unknown provider" in result.output


@pytest.mark.unit
def test_key_use_without_saved_key_exits_one(monkeypatch):
    import argus.cli.cmd_key as cmd_key

    monkeypatch.setattr(cmd_key.user_config, "resolve_key", lambda p: None)
    result = CliRunner().invoke(app, ["key", "use", "anthropic"])
    assert result.exit_code == 1, result.output
    assert "No anthropic key set" in result.output


@pytest.mark.unit
def test_key_use_with_saved_key_exits_zero_and_activates(monkeypatch):
    import argus.cli.cmd_key as cmd_key

    monkeypatch.setattr(cmd_key.user_config, "resolve_key", lambda p: "sk-test-1234")
    activated = []
    monkeypatch.setattr(cmd_key.user_config, "set_provider", lambda p: activated.append(p))
    result = CliRunner().invoke(app, ["key", "use", "anthropic"])
    assert result.exit_code == 0, result.output
    assert "Active provider set to" in result.output
    assert activated == ["anthropic"]
