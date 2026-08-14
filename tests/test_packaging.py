"""Packaging contract: default pip install ships CLI + LangGraph + UI."""
from __future__ import annotations

from importlib.metadata import entry_points, requires
from pathlib import Path

import pytest

import argus


def _default_requirements() -> list[str]:
    reqs = requires("argus-agents") or []
    return [r for r in reqs if "extra ==" not in r]


def _extra_requirements(name: str) -> list[str]:
    reqs = requires("argus-agents") or []
    markers = (f'extra == "{name}"', f"extra == '{name}'")
    return [r for r in reqs if any(m in r for m in markers)]


def _joined(reqs: list[str]) -> str:
    return " ".join(reqs).lower()


@pytest.mark.unit
def test_default_install_includes_cli_and_langgraph():
    default = _joined(_default_requirements())
    assert "typer" in default
    assert "rich" in default
    assert "langgraph" in default


@pytest.mark.unit
def test_default_install_does_not_pull_openai():
    default = _joined(_default_requirements())
    assert "openai" not in default


@pytest.mark.unit
def test_llm_extra_is_empty_noop():
    """Older `pip install argus-agents[llm]` still resolves; it installs nothing extra."""
    llm = _joined(_extra_requirements("llm"))
    assert "openai" not in llm
    assert "dotenv" not in llm


@pytest.mark.unit
def test_argus_console_script_registered():
    eps = entry_points()
    if hasattr(eps, "select"):
        scripts = list(eps.select(group="console_scripts"))
    else:
        scripts = list(eps.get("console_scripts", []))  # type: ignore[arg-type]
    matching = [e for e in scripts if e.name == "argus"]
    assert matching, "argus console script must be registered"
    assert "argus.cli.main" in matching[0].value


@pytest.mark.unit
def test_ui_assets_ship_in_package():
    dist = Path(argus.__file__).resolve().parent / "ui_dist"
    assert dist.is_dir(), "src/argus/ui_dist must ship with the package"
    assert (dist / "index.html").is_file()


@pytest.mark.unit
def test_doctor_langgraph_missing_points_at_default_install(monkeypatch):
    import builtins
    import sys

    import argus.cli.cmd_doctor as d

    for key in list(sys.modules):
        if key == "langgraph" or key.startswith("langgraph."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "langgraph" or name.startswith("langgraph."):
            raise ImportError("mocked missing langgraph")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok, msg = d._check_langgraph()
    assert ok is False
    assert "[langgraph]" not in msg
    assert "[cli]" not in msg
    assert "argus-agents" in msg


@pytest.mark.unit
def test_doctor_does_not_suggest_install_extras():
    import argus.cli.cmd_doctor as d

    source = Path(d.__file__).read_text(encoding="utf-8")
    assert "[cli]" not in source
    assert "[langgraph]" not in source
    assert "[llm]" not in source


@pytest.mark.unit
def test_doctor_warns_on_unrelated_argus_package(monkeypatch):
    import importlib.metadata as md

    import argus.cli.cmd_doctor as d

    real_distribution = md.distribution

    def fake_distribution(name: str):
        if name == "argus":
            return object()
        return real_distribution(name)

    monkeypatch.setattr(md, "distribution", fake_distribution)
    ok, msg = d._check_package_identity()
    assert ok is False
    assert "argus-agents" in msg
    assert "uninstall" in msg.lower()


@pytest.mark.unit
def test_doctor_package_identity_ok_without_unrelated_argus():
    import argus.cli.cmd_doctor as d

    ok, msg = d._check_package_identity()
    assert ok is True
    assert "argus-agents" in msg


@pytest.mark.unit
def test_cli_import_error_mentions_default_package_not_cli_extra():
    from argus.cli import main as cli_main

    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    assert "pip install argus-agents" in source
    assert "argus-agents[cli]" not in source
    assert "not 'argus'" in source

@pytest.mark.unit
def test_setup_prompt_does_not_require_typeddict():
    """AI setup prompt must attach ARGUS without rewriting state to TypedDict."""
    guide = Path(__file__).resolve().parents[1] / "website/app/guide/guide-content.tsx"
    prompt = guide.read_text(encoding="utf-8")
    assert "Do not convert plain-dict state to TypedDict" in prompt
    assert "Convert plain dict state to TypedDict" not in prompt
    assert "semantic_judge is off" in prompt

