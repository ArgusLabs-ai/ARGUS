"""What `argus replay` means, per run kind (#79).

The decision this encodes: a rerun's *state* always comes from the ledger, and
its *code* comes from exactly two places — the caller's graph (`--app`), or
references the run recorded about itself at record time (the legacy wrap path).
Replay never goes looking for the code it was not given.

One test per branch, plus the branch that must stay gone: replay used to scan
the project with an LLM to guess where a node's function lived and import it.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
import typer

from argus.cli.cmd_replay import replay_run
from argus.recorder import ArgusRecorder
from argus.replay import ReplayEngine
from argus.storage import load_run, save_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    query: str
    docs: list
    summary: str


def _graph(summarize=None):
    def search(s: _S) -> dict:
        return {"docs": [f"doc about {s['query']}"]}

    def _default(s: _S) -> dict:
        return {"summary": f"summary of {len(s.get('docs', []))} docs"}

    g = StateGraph(_S)
    g.add_node("search", search)
    g.add_node("summarize", summarize or _default)
    g.add_edge(START, "search")
    g.add_edge("search", "summarize")
    g.add_edge("summarize", END)
    return g.compile()


def _trace_run() -> str:
    """A recorder run: fat trace, no function references anywhere in it."""
    rec = ArgusRecorder()
    rec.attach(_graph()).invoke({"query": "refunds"})
    return rec.session.run_id


# ── branch 1: a trace run ────────────────────────────────────────────────────


@pytest.mark.integration
def test_a_trace_run_reruns_a_node_against_the_callers_graph():
    """The endorsed path: state from the notebook, code from the live app."""
    run_id = _trace_run()
    assert load_run(run_id).node_fn_refs in (None, {}), "a trace stores no code"

    def fixed(s: _S) -> dict:
        return {"summary": f"FIXED: {len(s.get('docs', []))} docs"}

    new_id = ReplayEngine().replay_live(run_id, "summarize", app=_graph(fixed))

    replayed = load_run(new_id)
    assert replayed.parent_run_id == run_id
    step = replayed.steps[0]
    assert step.node_name == "summarize"
    # Input came from the ledger row — `search` was never re-executed.
    assert step.input_state["docs"] == ["doc about refunds"]
    # Output came from the graph the caller passed, not from the run file.
    assert step.output_dict["summary"].startswith("FIXED:")


@pytest.mark.integration
def test_a_trace_run_without_a_graph_refuses_and_names_both_routes(capsys):
    """No `--app`: an error, not a cheerful exit 0, and it says what to do."""
    run_id = _trace_run()

    with pytest.raises(typer.Exit) as exc:
        replay_run(run_id, "summarize", app_module_str=None, only=True)
    assert exc.value.exit_code == 1

    out = capsys.readouterr().out
    assert "--app" in out, "the rerun route must be offered"
    assert f"argus check {run_id}" in out, "the no-graph route must be offered"


@pytest.mark.integration
def test_replay_never_goes_looking_for_a_traces_functions(monkeypatch):
    """The deleted third path: manufacture the refs by scanning the project.

    A wrong guess re-runs some other function and reports it as your node, and
    it used to happen silently, with an LLM call, on any run lacking refs.
    """
    import argus.source_locator as locator

    def _boom(*a, **k):
        raise AssertionError("replay must not hunt for node sources (#79)")

    monkeypatch.setattr(locator, "locate_node_sources", _boom)
    monkeypatch.setattr(locator, "derive_node_fn_refs", _boom)

    run_id = _trace_run()
    engine = ReplayEngine()

    with pytest.raises(ValueError, match="argus check"):
        engine.replay_node(run_id, "summarize")
    with pytest.raises(ValueError, match="argus check"):
        engine.replay(run_id, "summarize")


# ── branch 2: a legacy watcher run ───────────────────────────────────────────


def _node_for_legacy(s: _S) -> dict:
    """Module-level so a stored `module:function` reference can import it."""
    return {"summary": f"legacy summary of {len(s.get('docs', []))} docs"}


@pytest.mark.integration
def test_a_watcher_run_still_replays_from_the_refs_it_recorded():
    """Legacy wrap path: the run recorded where its own functions live."""
    run_id = _trace_run()
    record = load_run(run_id)
    # What ArgusWatcher captures at record time (watcher._capture_node_fn_refs).
    record.node_fn_refs = {"summarize": f"{__name__}:_node_for_legacy"}
    save_run(record)

    new_id = ReplayEngine().replay_node(run_id, "summarize")

    step = load_run(new_id).steps[0]
    assert step.node_name == "summarize"
    assert step.output_dict["summary"].startswith("legacy summary")
    assert step.input_state["docs"] == ["doc about refunds"], "input still from the ledger"
