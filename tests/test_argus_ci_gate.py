"""CI eat-own-cooking gate: must pass under ``pytest --argus``.

These tests intentionally rely on the pytest plugin's auto-instrumentation
(no explicit ``ArgusWatcher``). They are skipped unless ``--argus`` is set so
the default suite stays green; CI runs this file with ``--argus`` so a broken
plugin (no attach / false positive) fails ARGUS's own pipeline.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph

from argus.storage import last_run_id, list_runs, load_run

pytest.importorskip("langgraph")


class _Count(TypedDict):
    n: int


def _clean_graph() -> StateGraph:
    g = StateGraph(_Count)
    g.add_node("inc", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("inc")
    g.add_edge("inc", END)
    return g


@pytest.fixture(autouse=True)
def _no_engine_patching(monkeypatch: pytest.MonkeyPatch) -> None:
    """#78: the gate records through the callback hook, never the wrap path."""

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("pytest --argus reached patch_graph — the wrap path is back")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _require_argus(request: pytest.FixtureRequest) -> None:
    # default=False: skip instead of crashing when the pytest11 plugin is not loaded
    # (stale venv, or pytest without `pip install -e .`).
    if not request.config.getoption("--argus", default=False):
        pytest.skip("requires pytest --argus (CI eat-own-cooking gate; see #56)")


@pytest.mark.unit
def test_ci_argus_gate_clean_sync_invoke_creates_clean_run(
    request: pytest.FixtureRequest,
) -> None:
    """Sync invoke under --argus must produce a clean run (plugin must attach)."""
    _require_argus(request)

    before = {row["run_id"] for row in list_runs() if row.get("run_id")}
    app = _clean_graph().compile()
    assert app.invoke({"n": 0})["n"] == 1

    run_id = last_run_id()
    assert run_id is not None, "pytest --argus did not create a run (plugin attach broken?)"
    assert run_id not in before, "expected a new run from this invoke"

    record = load_run(run_id)
    assert record.overall_status == "clean"
