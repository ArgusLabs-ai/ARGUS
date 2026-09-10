"""Spike 1: the fat-trace recorder grades a run with no engine wrap.

Definition of done from the pivot brief §10: a demo graph whose node returns an
empty update fails the build, without `patcher.patch_graph`.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.recorder import ArgusRecorder, IncompleteTraceError
from argus.storage import list_runs, load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    query: str
    docs: list
    summary: str
    answer: str


def _build_app(summarize_returns: dict):
    def search(state: _S) -> dict:
        return {"docs": ["doc-1", "doc-2"]}

    def summarize(state: _S) -> dict:
        return dict(summarize_returns)

    def answer(state: _S) -> dict:
        return {"answer": f"answer from {state.get('summary', '(nothing)')}"}

    g = StateGraph(_S)
    g.add_node("search", search)
    g.add_node("summarize", summarize)
    g.add_node("answer", answer)
    g.add_edge(START, "search")
    g.add_edge("search", "summarize")
    g.add_edge("summarize", "answer")
    g.add_edge("answer", END)
    return g.compile()


def _no_patching(monkeypatch):
    """Any use of the old wrap path is a hard failure for this branch."""

    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the recorder must not wrap the engine")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


@pytest.mark.integration
def test_empty_update_fails_the_gate_without_patch_graph(monkeypatch):
    """The proof: a silent no-op fails `argus check`, blamed on its own node."""
    _no_patching(monkeypatch)

    app = _build_app({})
    recorder = ArgusRecorder()
    monitored = recorder.attach(app)

    result = monitored.invoke({"query": "q"})
    assert result["answer"], "the graph itself succeeded — that is the point"

    record = load_run(recorder.session.run_id)
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert "summarize" in verdict.failing_nodes
    assert "answer" not in verdict.failing_nodes, "blame belongs to the origin, not the victim"

    empty = [f for f in record.findings if f.type == "empty_output"]
    assert empty and empty[0].node == "summarize"
    assert empty[0].severity == "critical"


@pytest.mark.integration
def test_recorder_does_not_rebind_runtime_methods(monkeypatch):
    """The old path rebinds invoke/stream/batch on the compiled app. This one must not."""
    _no_patching(monkeypatch)

    app = _build_app({})
    before = {m: getattr(app, m) for m in ("invoke", "ainvoke", "stream", "astream", "batch")}

    ArgusRecorder().attach(app)

    for name, original in before.items():
        assert getattr(app, name) == original, f"{name} was rebound on the compiled app"
    assert not hasattr(app, "_argus_auto_persist")


@pytest.mark.integration
def test_the_update_is_recorded_not_the_merged_state(monkeypatch):
    """Fat, not skinny: the step's own return value survives, unmerged.

    This is the single assertion that catches a regression to storing "state
    after" — where `{}` would look like a state still full of `docs`.
    """
    _no_patching(monkeypatch)

    recorder = ArgusRecorder()
    recorder.attach(_build_app({})).invoke({"query": "q"})

    steps = {s.node_name: s for s in load_run(recorder.session.run_id).steps}

    assert steps["summarize"].output_dict == {}, "the update was merged away"
    assert steps["summarize"].input_state.get("docs") == ["doc-1", "doc-2"]
    assert steps["search"].output_dict == {"docs": ["doc-1", "doc-2"]}


@pytest.mark.integration
def test_a_real_update_stays_clean(monkeypatch):
    """Guards against a recorder that simply fails everything."""
    _no_patching(monkeypatch)

    recorder = ArgusRecorder()
    recorder.attach(_build_app({"summary": "a real summary of doc-1 and doc-2"})).invoke(
        {"query": "q"}
    )

    record = load_run(recorder.session.run_id)
    assert record.overall_status == "clean"
    assert evaluate_run(record).passed is True


@pytest.mark.integration
def test_parallel_fan_out_records_every_branch(monkeypatch):
    """Branches run on separate threads — each still gets its own step and its own update."""
    _no_patching(monkeypatch)

    def start(state: _S) -> dict:
        return {"docs": ["d"]}

    def join(state: _S) -> dict:
        return {"answer": "joined"}

    g = StateGraph(_S)
    g.add_node("start", start)
    g.add_node("left", lambda s: {"summary": "left"})
    g.add_node("right", lambda s: {})  # only this branch is silent
    g.add_node("join", join)
    g.add_edge(START, "start")
    g.add_edge("start", "left")
    g.add_edge("start", "right")
    g.add_edge("left", "join")
    g.add_edge("right", "join")
    g.add_edge("join", END)

    recorder = ArgusRecorder()
    recorder.attach(g.compile()).invoke({"query": "q"})

    record = load_run(recorder.session.run_id)
    steps = {s.node_name: s for s in record.steps}
    assert set(steps) == {"start", "left", "right", "join"}
    assert steps["right"].output_dict == {}
    assert steps["left"].output_dict == {"summary": "left"}

    blamed = {f.node for f in record.findings if f.type == "empty_output"}
    assert blamed == {"right"}


@pytest.mark.integration
def test_a_crash_is_recorded_and_fails_the_gate(monkeypatch):
    """The error path: on_chain_error still closes the step and grades the run."""
    _no_patching(monkeypatch)

    def boom(state: _S) -> dict:
        raise KeyError("summary")

    g = StateGraph(_S)
    g.add_node("search", lambda s: {"docs": ["d"]})
    g.add_node("boom", boom)
    g.add_edge(START, "search")
    g.add_edge("search", "boom")
    g.add_edge("boom", END)

    recorder = ArgusRecorder()
    with pytest.raises(KeyError):
        recorder.attach(g.compile()).invoke({"query": "q"})

    record = load_run(recorder.session.run_id)
    assert record.overall_status == "crashed"
    assert record.first_failure_step == "boom"
    assert evaluate_run(record).passed is False


@pytest.mark.unit
def test_an_empty_trace_refuses_to_grade():
    """Brief §4: an incomplete recording is never 'no findings, so it passed'."""
    recorder = ArgusRecorder()
    recorder.attach(_build_app({}))

    with pytest.raises(IncompleteTraceError):
        recorder._finish()

    # and it must not leave a "clean" run behind for the gate to pass on
    assert not list_runs()


@pytest.mark.unit
def test_invoking_before_attach_is_an_error():
    with pytest.raises(RuntimeError, match="attach"):
        ArgusRecorder()._require_session()
