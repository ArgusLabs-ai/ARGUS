"""Spike 3: a field D needs is blamed on A, not on C standing next to D.

A → B → C → D. D reads `b`. B and C never had a duty to produce it.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402

CONSUMERS = {"b": ["D"]}


class _S(TypedDict, total=False):
    seed: str
    b: str
    noise_b: str
    noise_c: str
    answer: str


def _no_patching(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the recorder must not wrap the engine")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _run(a_writes_b: bool, drop_at_c: bool = False):
    def a(state: _S) -> dict:
        return {"b": "ok"} if a_writes_b else {"noise_a": "unrelated"}

    def b(state: _S) -> dict:
        return {"noise_b": "unrelated"}

    def c(state: _S) -> dict:
        return {"b": None} if drop_at_c else {"noise_c": "unrelated"}

    def d(state: _S) -> dict:
        return {"answer": f"used {state.get('b')}"}

    g = StateGraph(_S)
    for name, fn in (("A", a), ("B", b), ("C", c), ("D", d)):
        g.add_node(name, fn)
    g.add_edge(START, "A")
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("C", "D")
    g.add_edge("D", END)

    recorder = ArgusRecorder(consumers=CONSUMERS)
    recorder.attach(g.compile()).invoke({"seed": "s"})
    # Everything asserted below comes off the file. No second invoke.
    return load_run(recorder.session.run_id)


@pytest.mark.integration
def test_field_written_by_a_and_read_by_d_is_clean(monkeypatch):
    _no_patching(monkeypatch)

    record = _run(a_writes_b=True)
    verdict = evaluate_run(record)

    assert verdict.passed is True
    assert verdict.failing_nodes == ()
    assert record.overall_status == "clean"


@pytest.mark.integration
def test_field_never_written_blames_a_not_c_or_d(monkeypatch):
    _no_patching(monkeypatch)

    record = _run(a_writes_b=False)
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert "A" in verdict.failing_nodes
    assert "C" not in verdict.failing_nodes, "C had no duty to produce b"
    assert "D" not in verdict.failing_nodes, "D is the victim, not the origin"

    miss = [f for f in record.findings if f.type == "missing_field" and f.field_path == "b"]
    assert [f.node for f in miss] == ["A"]
    assert miss[0].severity == "critical"

    # the reader that made `b` required is named on the step itself
    origin_step = next(s for s in record.steps if s.node_name == "A")
    assert "`D`" in origin_step.inspection.message


@pytest.mark.integration
def test_a_field_dropped_midway_blames_the_dropper(monkeypatch):
    """A wrote it, C threw it away — same rule, different origin."""
    _no_patching(monkeypatch)

    record = _run(a_writes_b=True, drop_at_c=True)
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert "C" in verdict.failing_nodes
    assert "A" not in verdict.failing_nodes


@pytest.mark.unit
def test_no_consumers_declared_means_no_contextual_findings(monkeypatch):
    """Without a declared reader the layer stays silent — it never guesses."""
    _no_patching(monkeypatch)

    def a(state: _S) -> dict:
        return {"noise_a": "unrelated"}

    def d(state: _S) -> dict:
        return {"answer": "fine"}

    g = StateGraph(_S)
    g.add_node("A", a)
    g.add_node("D", d)
    g.add_edge(START, "A")
    g.add_edge("A", "D")
    g.add_edge("D", END)

    recorder = ArgusRecorder()  # no consumers
    recorder.attach(g.compile()).invoke({"seed": "s"})

    assert evaluate_run(load_run(recorder.session.run_id)).passed is True


@pytest.mark.integration
def test_a_field_emptied_not_nulled_is_still_a_drop(monkeypatch):
    """`{"docs": []}` is the commonest real drop: a filter that removed everything.

    The old wrap path caught this via the successor's type hints. A trace has no
    type hints, so the declared consumer map has to carry it — and `_lacks` has
    to agree with the inspector on what "empty" means.
    """
    _no_patching(monkeypatch)

    def search(state: _S) -> dict:
        return {"b": "found something"}

    def clean(state: _S) -> dict:
        return {"b": ""}  # filtered it all away

    def use(state: _S) -> dict:
        return {"answer": f"used {state.get('b')!r}"}

    g = StateGraph(_S)
    for name, fn in (("search", search), ("clean", clean), ("use", use)):
        g.add_node(name, fn)
    g.add_edge(START, "search")
    g.add_edge("search", "clean")
    g.add_edge("clean", "use")
    g.add_edge("use", END)

    recorder = ArgusRecorder(consumers={"b": ["use"]})
    recorder.attach(g.compile()).invoke({"seed": "s"})

    verdict = evaluate_run(load_run(recorder.session.run_id))
    assert verdict.passed is False
    assert "clean" in verdict.failing_nodes, "the node that emptied it is the origin"
    assert "search" not in verdict.failing_nodes, "search did its job"


@pytest.mark.integration
def test_a_field_filled_in_the_middle_is_clean(monkeypatch):
    """Progressive fill: A has no `b` yet, B writes it, D reads it.

    Walking forward from step 0 and stopping at the first row that lacks `b`
    blames A for not having done B's job. Blame is anchored at the reader.
    """
    _no_patching(monkeypatch)

    g = StateGraph(_S)
    g.add_node("A", lambda s: {"seed": "ready"})  # no `b` yet — normal
    g.add_node("B", lambda s: {"b": "written here"})
    g.add_node("C", lambda s: {"noise_c": "unrelated"})
    g.add_node("D", lambda s: {"answer": f"used {s.get('b')}"})
    g.add_edge(START, "A")
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("C", "D")
    g.add_edge("D", END)

    recorder = ArgusRecorder(consumers={"b": ["D"]})
    recorder.attach(g.compile()).invoke({"seed": "s"})

    record = load_run(recorder.session.run_id)
    assert evaluate_run(record).passed is True
    assert record.overall_status == "clean"
    assert [f for f in record.findings if f.type == "missing_field"] == []


@pytest.mark.integration
def test_a_reader_that_writes_the_field_itself_is_clean(monkeypatch):
    """An accumulator reads and produces the same field. Nothing is missing."""
    _no_patching(monkeypatch)

    g = StateGraph(_S)
    g.add_node("A", lambda s: {"noise_a": "setup"})
    g.add_node("D", lambda s: {"b": "created by the reader itself"})
    g.add_edge(START, "A")
    g.add_edge("A", "D")
    g.add_edge("D", END)

    recorder = ArgusRecorder(consumers={"b": ["D"]})
    recorder.attach(g.compile()).invoke({"seed": "s"})

    record = load_run(recorder.session.run_id)
    assert evaluate_run(record).passed is True
    assert [f for f in record.findings if f.type == "missing_field"] == []


@pytest.mark.integration
def test_parallel_branch_writes_the_field_before_the_join_reads_it(monkeypatch):
    """Only one branch produces `b`; the join consumes it. Siblings are not at fault."""
    _no_patching(monkeypatch)

    g = StateGraph(_S)
    g.add_node("start", lambda s: {"noise_a": "go"})
    g.add_node("left", lambda s: {"b": "from left"})
    g.add_node("right", lambda s: {"noise_c": "unrelated"})
    g.add_node("join", lambda s: {"answer": f"read {s.get('b')}"})
    g.add_edge(START, "start")
    g.add_edge("start", "left")
    g.add_edge("start", "right")
    g.add_edge("left", "join")
    g.add_edge("right", "join")
    g.add_edge("join", END)

    recorder = ArgusRecorder(consumers={"b": ["join"]})
    recorder.attach(g.compile()).invoke({"seed": "s"})

    record = load_run(recorder.session.run_id)
    assert evaluate_run(record).passed is True
    assert [f for f in record.findings if f.type == "missing_field"] == []


@pytest.mark.integration
def test_a_field_supplied_by_the_caller_is_not_missing(monkeypatch):
    """`seed` comes in with invoke() and no node writes it. That is not a drop."""
    _no_patching(monkeypatch)

    g = StateGraph(_S)
    g.add_node("A", lambda s: {"noise_a": "untouched"})
    g.add_node("D", lambda s: {"answer": f"read {s.get('seed')}"})
    g.add_edge(START, "A")
    g.add_edge("A", "D")
    g.add_edge("D", END)

    recorder = ArgusRecorder(consumers={"seed": ["D"]})
    recorder.attach(g.compile()).invoke({"seed": "from the caller"})

    record = load_run(recorder.session.run_id)
    assert evaluate_run(record).passed is True
    assert [f for f in record.findings if f.type == "missing_field"] == []
