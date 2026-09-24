"""B-2 (F-16/F-39): library code must not call load_dotenv(override=True).

`override=True` lets a library's ambient `.env` load silently replace keys the
host application already set in the process environment. The `.env` loads stay
(python-dotenv default `override=False` still fills unset keys); only the
override is forbidden. Pinned two ways: an AST scan over every src module
(catches all present and future call sites) and a behavioral check that the
load path leaves a planted process-env value alone.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "argus"


def _load_dotenv_call_sites() -> list[str]:
    offenders: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "load_dotenv":
                continue
            for kw in node.keywords:
                if kw.arg == "override" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    offenders.append(f"{path.relative_to(SRC_DIR.parent.parent)}:{node.lineno}")
    return offenders


def test_no_load_dotenv_override_true_in_src() -> None:
    offenders = _load_dotenv_call_sites()
    assert offenders == [], f"load_dotenv(override=True) call sites: {offenders}"


def test_llm_generalize_leaves_process_env_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioral pin: the load path runs with override unset/False, so a
    planted process-env value survives the call (fake dotenv faithfully
    replicates override semantics — it would clobber only if told to)."""
    calls: list[dict] = []

    def _fake_load_dotenv(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("override"):
            os.environ["ARGUS_B2_SENTINEL"] = "from-dotenv"
        return True

    fake_module = type(sys)("dotenv")
    fake_module.load_dotenv = _fake_load_dotenv  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dotenv", fake_module)
    monkeypatch.setenv("ARGUS_B2_SENTINEL", "process")

    from argus.signature_generalizer import _llm_generalize

    _llm_generalize("some-pattern", ())  # returns None offline; the load path still runs

    assert calls, "expected the .env load path to execute"
    assert all(kw.get("override") is not True for kw in calls)
    assert os.environ["ARGUS_B2_SENTINEL"] == "process"
