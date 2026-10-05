"""Does the notebook hold everything the trace gave it, and hand it back intact?

Two questions, one graph. First: every column of every row, checked against the
recorded `NodeEvent` it came from — no column silently dropped on the way in,
none altered on the way through the run file. Second: the ledger as a reload
source for `argus replay`, which re-feeds a step's recorded input.

The existing round-trip test compares four of the seven columns. `status` was
not one of them, which is how a node that never ran stayed in the notebook.
"""

from __future__ import annotations

import dataclasses
import operator
from typing import Annotated, TypedDict

import pytest

from argus.ledger import build_ledger, reducer_kinds
from argus.recorder import ArgusRecorder
from argus.replay import ReplayEngine
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    query: str
    docs: Annotated[list, operator.add]
    route: str
    notes: str
    bulky: str
    answer: str


@tool
def good_tool(q: str) -> list:
    """A tool that returns something."""
    return [f"doc for {q}"]


@tool
def bad_tool(q: str) -> list:
    """A tool that fails."""
    raise RuntimeError("upstream 503")


def _kitchen_sink_app():
    """One run covering every column the ledger claims to carry.

    `search` calls two tools, one of which errors. `fan` adds to a reduced
    field. `silent` returns the empty update. `bulky` returns a field over the
    size limit. A conditional skips `never`.
    """

    def search(s: _S) -> dict:
        docs = good_tool.invoke({"q": s["query"]})
        try:
            docs = docs + bad_tool.invoke({"q": s["query"]})
        except Exception:
            pass
        return {"docs": docs, "route": "take"}

    def fan(s: _S) -> dict:
        return {"docs": ["from-fan"]}

    def silent(s: _S) -> dict:
        return {}

    def bulky(s: _S) -> dict:
        return {"bulky": "z" * 200_000, "notes": "kept"}

    def take(s: _S) -> dict:
        return {"answer": f"{len(s['docs'])} docs"}

    def never(s: _S) -> dict:  # pragma: no cover - the branch is never taken
        return {"answer": "unreachable"}

    g = StateGraph(_S)
    for name, fn in (
        ("search", search),
        ("fan", fan),
        ("silent", silent),
        ("bulky", bulky),
        ("take", take),
        ("never", never),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "search")
    g.add_edge("search", "fan")
    g.add_edge("fan", "silent")
    g.add_edge("silent", "bulky")
    g.add_conditional_edges("bulky", lambda s: s["route"], {"take": "take", "skip": "never"})
    g.add_edge("take", END)
    g.add_edge("never", END)
    return g.compile()


def _recorded() -> tuple[str, object]:
    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(_kitchen_sink_app()).invoke({"query": "silent failures", "docs": []})
    return recorder.session.run_id, recorder.session


# ── 1. fidelity ─────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_every_row_matches_the_event_it_came_from():
    run_id, _ = _recorded()
    record = load_run(run_id)
    rows = build_ledger(record.steps, record.initial_state, record.reducer_kinds)

    ran = [e for e in record.steps if e.status != "skipped"]
    assert len(rows) == len(ran), "one row per step that ran, no more, no less"

    for row, event in zip(rows, ran):
        assert row.step_index == event.step_index
        assert row.node == event.node_name
        assert row.status == event.status
        assert row.input_state == event.input_state
        assert row.update == event.output_dict
        assert row.error == event.exception
        assert row.tools == event.tool_calls


@pytest.mark.integration
def test_the_whole_row_survives_the_run_file():
    """All seven columns, not the four the older round-trip test compared."""
    run_id, session = _recorded()
    record = load_run(run_id)

    live = build_ledger(session._events, session._initial_state, session.reducer_kinds)
    reloaded = build_ledger(record.steps, record.initial_state, record.reducer_kinds)

    assert [dataclasses.asdict(r) for r in live] == [dataclasses.asdict(r) for r in reloaded]


@pytest.mark.integration
def test_the_notebook_carries_the_evidence_each_layer_reads():
    run_id, _ = _recorded()
    rows = {r.node: r for r in build_ledger(*_ledger_args(run_id))}

    # tool I/O, including the failure — the tool-failure scan's evidence
    tools = {t["name"]: t for t in rows["search"].tools}
    assert set(tools) == {"good_tool", "bad_tool"}
    assert tools["good_tool"]["output"] == ["doc for silent failures"]
    assert tools["good_tool"]["error"] is None
    assert "503" in tools["bad_tool"]["error"]
    assert tools["bad_tool"]["output"] is None

    # the empty update — `empty_output`'s evidence — distinct from "no update"
    assert rows["silent"].update == {}
    assert rows["silent"].update is not None

    # the reduced field really accumulated, so `take` read what the graph gave it
    assert rows["take"].input_state["docs"] == rows["fan"].state_after["docs"]
    assert "from-fan" in rows["take"].input_state["docs"]

    # an oversized field is a marker, not a silent truncation to ""
    assert rows["bulky"].update["bulky"]["__argus_truncated__"] is True
    assert rows["bulky"].update["notes"] == "kept"

    # the untaken branch is not a step
    assert "never" not in rows


# ── 2. replay reloads through the same rows ─────────────────────────────────


@pytest.mark.integration
def test_the_ledger_is_the_state_replay_re_feeds():
    """`replay_node` re-runs a node on its recorded input. That is a row.

    If the two ever disagree, the notebook is describing a run that replay does
    not reproduce.
    """
    run_id, _ = _recorded()
    record = load_run(run_id)
    rows = {r.node: r for r in build_ledger(*_ledger_args(run_id))}

    for event in record.steps:
        if event.status == "skipped":
            continue
        assert rows[event.node_name].input_state == event.input_state


@pytest.mark.integration
def test_replaying_a_trace_recorded_run_refuses_instead_of_running_a_placeholder():
    """A recorder run has no node function refs — only the wrap path captures those.

    The recorder registers unannotated placeholders so the successor rules still
    fire; replay must not import one and "re-run" it.
    """
    run_id, _ = _recorded()

    with pytest.raises(ValueError, match="node_fn_refs|function reference|Re-record"):
        ReplayEngine().replay_node(run_id, "search")


@pytest.mark.integration
def test_a_failed_replay_leaves_the_source_notebook_untouched():
    run_id, _ = _recorded()
    before = [dataclasses.asdict(r) for r in build_ledger(*_ledger_args(run_id))]

    with pytest.raises(Exception):
        ReplayEngine().replay_node(run_id, "search")

    after = [dataclasses.asdict(r) for r in build_ledger(*_ledger_args(run_id))]
    assert after == before


def _ledger_args(run_id: str):
    record = load_run(run_id)
    return record.steps, record.initial_state, record.reducer_kinds


@pytest.mark.unit
def test_reducer_kinds_round_trips_onto_the_record():
    run_id, session = _recorded()
    record = load_run(run_id)
    assert record.reducer_kinds == session.reducer_kinds
    assert record.reducer_kinds.get("docs") == "add"
    assert reducer_kinds(None) == {}


# ── 3. a replay that actually runs: the reload, and the source run after it ──

_REPLAYABLE = "argus_ledger_replay_pipeline"

_PIPELINE_SRC = '''
"""Throwaway pipeline for the ledger's replay tests."""

def fetch(state):
    return {"docs": ["d1", "d2"], "status": "PLACEHOLDER"}


def transform(state):
    return {"summary": "docs=%d status=%s" % (len(state["docs"]), state["status"])}


def finish(state):
    return {"done": True, "final": state["summary"]}
'''

_ORDER = ["fetch", "transform", "finish"]


@pytest.fixture
def replayable(tmp_path, monkeypatch):
    """A run recorded on the wrap path — the only one replay can re-execute."""
    import importlib
    import sys

    from argus.models import LLMInvestigationConfig
    from argus.session import ArgusSession

    monkeypatch.chdir(tmp_path)
    (tmp_path / f"{_REPLAYABLE}.py").write_text(_PIPELINE_SRC, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)
    sys.modules.pop(_REPLAYABLE, None)
    mod = importlib.import_module(_REPLAYABLE)

    session = ArgusSession(llm_investigation=LLMInvestigationConfig(enabled=False))
    session.set_node_names(list(_ORDER))
    session.set_edges({"fetch": ["transform"], "transform": ["finish"]})
    session.node_fn_refs = {n: f"{_REPLAYABLE}:{n}" for n in _ORDER}
    session.node_fn_paths = {n: f"{_REPLAYABLE}.py" for n in _ORDER}

    state: dict = {"query": "hello"}
    for name in _ORDER:
        state = {**state, **session.wrap(name, getattr(mod, name))(state)}
    session.finalize()

    yield session.run_id
    sys.modules.pop(_REPLAYABLE, None)


@pytest.mark.integration
def test_replay_re_feeds_exactly_what_the_notebook_recorded(replayable):
    """The row is the reload: replay's input for a node is that row's input."""
    rows = {r.node: r for r in build_ledger(*_ledger_args(replayable))}

    new_id = ReplayEngine().replay_node(replayable, "transform")

    replayed = {r.node: r for r in build_ledger(*_ledger_args(new_id))}
    assert replayed["transform"].input_state == rows["transform"].input_state
    assert replayed["transform"].update == rows["transform"].update


@pytest.mark.integration
def test_a_patched_replay_reloads_the_row_and_changes_only_the_patched_field(replayable):
    before = {r.node: r for r in build_ledger(*_ledger_args(replayable))}
    assert before["transform"].input_state["status"] == "PLACEHOLDER"

    new_id = ReplayEngine().replay(replayable, "transform", patch={"set": {"status": "OK"}})

    after = {r.node: r for r in build_ledger(*_ledger_args(new_id))}
    assert after["transform"].input_state["status"] == "OK"
    assert after["transform"].input_state["docs"] == before["transform"].input_state["docs"]
    assert after["transform"].update["summary"] == "docs=2 status=OK"
    assert after["finish"].input_state["summary"] == "docs=2 status=OK"


@pytest.mark.integration
def test_replay_does_not_disturb_the_notebook_it_replayed_from(replayable):
    before = [dataclasses.asdict(r) for r in build_ledger(*_ledger_args(replayable))]

    new_id = ReplayEngine().replay(replayable, "transform", patch={"set": {"status": "OK"}})

    assert [dataclasses.asdict(r) for r in build_ledger(*_ledger_args(replayable))] == before
    assert load_run(new_id).parent_run_id == replayable
    assert load_run(new_id).run_id != replayable


@pytest.mark.integration
def test_a_replay_run_starts_its_notebook_at_the_replayed_node(replayable):
    """Upstream nodes are not re-run, so they are not rows. The notebook says so."""
    new_id = ReplayEngine().replay(replayable, "transform", patch={"set": {"status": "OK"}})

    rows = build_ledger(*_ledger_args(new_id))
    assert [r.node for r in rows] == ["transform", "finish"]
    assert load_run(new_id).replay_from_step == "transform"
