"""Does the pivot path catch the silent failures enterprises actually ship?

Real LangGraph pipelines — a supervisor loop, a map-reduce fan-out, a CRM
triage chain, a tool-calling fetcher, a subgraph — run through
``ArgusRecorder`` with nothing patched. Each test asserts the *verdict and the
blamed node*, not just "something was found": blaming the crash site instead of
the origin is the bug this architecture exists to fix, so a test that only
checks `passed is False` would pass on the old behaviour too.

Detection only. Nothing here reruns or fixes a node.

Six of these started life as ``xfail(strict=True)`` — reproducible gaps this
file found in the pivot path (routers hiding ``empty_output``, subgraph nodes
never graded, crash origin lost without a declared contract, ``answer: "N/A"``
grading clean, lorem ipsum unmatched, contextual's reason discarded). All six
are fixed; the tests stay as the regression guard. See ``docs/PIVOT-BRANCH.md``.
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, TypedDict

import pytest

from argus.check import evaluate_run
from argus.ledger import build_ledger
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

pytestmark = pytest.mark.integration


# ── harness ──────────────────────────────────────────────────────────────────


def _run(app, payload, consumers=None, *, is_async=False, expect_raise=None):
    """Attach, invoke, return (verdict, record, ledger rows by node)."""
    recorder = ArgusRecorder(consumers=consumers, semantic_judge=False)
    bound = recorder.attach(app)
    if expect_raise is not None:
        with pytest.raises(expect_raise):
            bound.invoke(payload)
    elif is_async:
        asyncio.run(bound.ainvoke(payload))
    else:
        bound.invoke(payload)
    record = load_run(recorder.session.run_id)
    rows = build_ledger(record.steps, record.initial_state, record.reducer_kinds)
    return evaluate_run(record), record, {r.node: r for r in rows}


def _no_patching(monkeypatch):
    """The pivot path must never touch the graph engine."""

    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    _no_patching(monkeypatch)


def _finding_nodes(record, type_):
    return {f.node for f in record.findings if f.type == type_}


# ── pipeline 1: supervisor / worker loop (cyclic, conditional) ───────────────


class SupervisorState(TypedDict, total=False):
    task: str
    rounds: int
    findings: Annotated[list[str], operator.add]
    report: str


def _supervisor_pipeline(worker, *, worker_routes: bool):
    """supervisor → worker → (loop back | compile).

    ``worker_routes`` decides whether the worker owns the conditional edge
    (the usual agent-loop shape) or a plain edge leads on.
    """

    def supervisor(state: SupervisorState) -> dict:
        return {"rounds": state.get("rounds", 0) + 1}

    def compile_report(state: SupervisorState) -> dict:
        return {"report": "; ".join(state.get("findings") or ["(nothing found)"])}

    def route(state: SupervisorState) -> str:
        return "supervisor" if state.get("rounds", 0) < 2 else "compile"

    g = StateGraph(SupervisorState)
    g.add_node("supervisor", supervisor)
    g.add_node("worker", worker)
    g.add_node("compile", compile_report)
    g.add_edge(START, "supervisor")
    g.add_edge("supervisor", "worker")
    if worker_routes:
        branches = {"supervisor": "supervisor", "compile": "compile"}
        g.add_conditional_edges("worker", route, branches)
    else:
        g.add_edge("worker", "compile")
    g.add_edge("compile", END)
    return g.compile()


def test_healthy_supervisor_loop_is_clean():
    """A loop that runs twice and accumulates is not a failure."""
    app = _supervisor_pipeline(
        lambda s: {"findings": [f"fact-{s.get('rounds')}"]}, worker_routes=True
    )
    verdict, record, rows = _run(app, {"task": "research"}, {"findings": ["compile"]})

    assert verdict.passed is True, verdict.reasons
    assert record.overall_status == "clean"
    assert rows["compile"].state_after["findings"] == ["fact-1", "fact-2"], (
        "operator.add fan-in must accumulate across loop iterations in the ledger"
    )


def test_worker_that_searches_and_returns_nothing_is_blamed_not_compile():
    """The worker writes `findings: []`. `compile` still produces a report."""
    app = _supervisor_pipeline(lambda s: {"findings": []}, worker_routes=True)
    verdict, record, _rows = _run(app, {"task": "research"}, {"findings": ["compile"]})

    assert verdict.passed is False
    assert "worker" in verdict.failing_nodes
    assert "compile" not in verdict.failing_nodes, "the reader is the victim, not the origin"
    assert "worker" in _finding_nodes(record, "empty_result")


def test_worker_no_op_on_a_plain_edge_is_caught():
    """A worker on a plain edge: the baseline `empty_output` case."""
    app = _supervisor_pipeline(lambda s: {}, worker_routes=False)
    verdict, record, _rows = _run(app, {"task": "research"}, {"findings": ["compile"]})

    assert verdict.passed is False
    assert "worker" in verdict.failing_nodes
    assert "worker" in _finding_nodes(record, "empty_output")


def test_worker_no_op_is_caught_even_when_the_worker_owns_the_loop_edge():
    """A router is still a node: owning the loop edge must not grant amnesty."""
    app = _supervisor_pipeline(lambda s: {}, worker_routes=True)
    verdict, _record, _rows = _run(app, {"task": "research"}, {"findings": ["compile"]})

    assert verdict.passed is False
    assert "worker" in verdict.failing_nodes


# ── pipeline 2: map-reduce fan-out ───────────────────────────────────────────


class MapReduceState(TypedDict, total=False):
    docs: list[str]
    summaries: Annotated[list[str], operator.add]
    final: str


def _mapreduce_pipeline(branch_b):
    def split(state: MapReduceState) -> dict:
        return {"docs": ["contract-a", "contract-b"]}

    def branch_a(state: MapReduceState) -> dict:
        return {"summaries": ["contract-a: 12 month term"]}

    def reduce_(state: MapReduceState) -> dict:
        return {"final": " | ".join(state.get("summaries") or [])}

    g = StateGraph(MapReduceState)
    for name, fn in (("split", split), ("a", branch_a), ("b", branch_b), ("reduce", reduce_)):
        g.add_node(name, fn)
    g.add_edge(START, "split")
    g.add_edge("split", "a")
    g.add_edge("split", "b")
    g.add_edge("a", "reduce")
    g.add_edge("b", "reduce")
    g.add_edge("reduce", END)
    return g.compile()


def test_healthy_fanout_is_clean_and_both_branches_land_in_the_ledger():
    app = _mapreduce_pipeline(lambda s: {"summaries": ["contract-b: auto-renews"]})
    verdict, _record, rows = _run(app, {}, {"summaries": ["reduce"]})

    assert verdict.passed is True, verdict.reasons
    assert len(rows["reduce"].state_after["summaries"]) == 2, "reducer fan-in must not overwrite"


def test_one_silent_branch_in_a_fanout_is_blamed_alone():
    """Branch b returns {}. `reduce` still produces a plausible-looking `final`."""
    app = _mapreduce_pipeline(lambda s: {})
    verdict, record, rows = _run(app, {}, {"summaries": ["reduce"]})

    assert verdict.passed is False
    assert "b" in verdict.failing_nodes
    assert "a" not in verdict.failing_nodes, "the healthy sibling must not be blamed"
    assert "reduce" not in verdict.failing_nodes, "the reader must not be blamed"
    assert "b" in _finding_nodes(record, "empty_output")
    assert rows["reduce"].state_after["summaries"] == ["contract-a: 12 month term"], (
        "half the work is missing but the run looks complete — the silent failure"
    )


# ── pipeline 3: CRM triage — the long-range contextual case ──────────────────


class CrmState(TypedDict, total=False):
    email: str
    customer_id: str
    tier: str
    tickets: list[str]
    sentiment: str
    reply: str


def _crm_pipeline(enrich):
    """identify → enrich → analyze → respond.

    `respond` reads `customer_id`, written three steps earlier. `analyze` sits
    in between and never had a duty to produce it — blaming `analyze` (adjacent
    matching) or `respond` (the crash site) are both wrong answers.
    """

    def identify(state: CrmState) -> dict:
        return {"customer_id": "cus_123", "tier": "gold"}

    def analyze(state: CrmState) -> dict:
        return {"sentiment": "negative"}

    def respond(state: CrmState) -> dict:
        return {"reply": f"Hi {state.get('customer_id')} ({state.get('tier')}), sorry about that."}

    g = StateGraph(CrmState)
    for name, fn in (
        ("identify", identify),
        ("enrich", enrich),
        ("analyze", analyze),
        ("respond", respond),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "identify")
    g.add_edge("identify", "enrich")
    g.add_edge("enrich", "analyze")
    g.add_edge("analyze", "respond")
    g.add_edge("respond", END)
    return g.compile()


CRM_CONSUMERS = {"customer_id": ["respond"], "tier": ["respond"]}


def test_healthy_crm_chain_is_clean():
    """Progressive fill: `identify` has no `sentiment` yet. Not a failure."""
    verdict, record, _rows = _run(
        _crm_pipeline(lambda s: {"tickets": ["t-1"]}), {"email": "a@b.c"}, CRM_CONSUMERS
    )

    assert verdict.passed is True, verdict.reasons
    assert record.findings == []


def test_a_middle_node_that_nulls_a_field_is_the_origin():
    """`enrich` writes customer_id=None. Three steps later `respond` needs it."""
    app = _crm_pipeline(lambda s: {"tickets": ["t-1"], "customer_id": None})
    verdict, record, _rows = _run(app, {"email": "a@b.c"}, CRM_CONSUMERS)

    assert verdict.passed is False
    assert verdict.failing_nodes == ("enrich",), "the dropper, not the reader or its neighbour"
    assert "respond" not in verdict.failing_nodes
    assert "analyze" not in verdict.failing_nodes
    missing = [f for f in record.findings if f.type == "missing_field"]
    assert missing and missing[0].node == "enrich"
    assert missing[0].field_path == "customer_id"


def test_a_field_emptied_to_a_blank_string_counts_as_dropped():
    """`tier: ""` is a drop, not a value — the commonest real-world variant."""
    app = _crm_pipeline(lambda s: {"tickets": ["t-1"], "tier": ""})
    verdict, record, _rows = _run(app, {"email": "a@b.c"}, {"tier": ["respond"]})

    assert verdict.passed is False
    assert "enrich" in verdict.failing_nodes
    assert any(f.field_path == "tier" for f in record.findings if f.type == "missing_field")


def test_the_async_path_grades_identically():
    """ainvoke must record and grade the same as invoke."""
    app = _crm_pipeline(lambda s: {"tickets": ["t-1"], "customer_id": None})
    verdict, _record, _rows = _run(app, {"email": "a@b.c"}, CRM_CONSUMERS, is_async=True)

    assert verdict.passed is False
    assert "enrich" in verdict.failing_nodes


# ── pipeline 4: tool-calling fetcher ─────────────────────────────────────────


class FetchState(TypedDict, total=False):
    query: str
    raw: dict
    answer: str


@tool
def orders_api(query: str) -> dict:
    """Look up orders. Returns an upstream error."""
    return {"status": 500, "error": "upstream timeout", "results": []}


@tool
def exploding_api(query: str) -> dict:
    """Look up orders. Raises."""
    raise RuntimeError("connection reset by peer")


def _fetch_pipeline(fetch):
    def answer(state: FetchState) -> dict:
        hits = (state.get("raw") or {}).get("results", [])
        return {"answer": f"Found {len(hits)} matching orders."}

    g = StateGraph(FetchState)
    g.add_node("fetch", fetch)
    g.add_node("answer", answer)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "answer")
    g.add_edge("answer", END)
    return g.compile()


def test_a_swallowed_http_500_is_blamed_on_the_fetcher():
    """The node stores the error payload and the graph answers confidently."""
    app = _fetch_pipeline(lambda s: {"raw": orders_api.invoke({"query": s.get("query", "")})})
    verdict, record, rows = _run(app, {"query": "recent orders"})

    assert verdict.passed is False
    assert "fetch" in verdict.failing_nodes
    assert "fetch" in _finding_nodes(record, "error_response")
    assert rows["answer"].update["answer"] == "Found 0 matching orders.", (
        "the pipeline shipped a confident answer over a failed call"
    )
    assert rows["fetch"].tools[0]["name"] == "orders_api", "tool I/O belongs on the fetcher's row"


def test_a_caught_tool_exception_still_fails_the_gate():
    """try/except around the tool hides it from the framework, not from ARGUS."""

    def fetch(state: FetchState) -> dict:
        try:
            return {"raw": exploding_api.invoke({"query": state.get("query", "")})}
        except Exception as exc:  # the pattern that produces silent failures
            return {"raw": {"error": str(exc), "results": []}}

    verdict, record, rows = _run(_fetch_pipeline(fetch), {"query": "recent orders"})

    assert verdict.passed is False
    assert "fetch" in verdict.failing_nodes
    assert "fetch" in _finding_nodes(record, "error_response")
    assert rows["fetch"].tools[0]["error"], "the raising tool's error is on the ledger row"


# ── pipeline 5: degraded model output ────────────────────────────────────────


class GenState(TypedDict, total=False):
    prompt: str
    context: list[str]
    answer: str
    final: str


def _gen_pipeline(text):
    g = StateGraph(GenState)
    g.add_node("prep", lambda s: {"context": ["ctx"]})
    g.add_node("generate", lambda s: {"answer": text})
    g.add_node("ship", lambda s: {"final": str(s.get("answer"))})
    g.add_edge(START, "prep")
    g.add_edge("prep", "generate")
    g.add_edge("generate", "ship")
    g.add_edge("ship", END)
    return g.compile()


def test_real_prose_is_not_flagged():
    verdict, record, _rows = _run(
        _gen_pipeline("Revenue grew 12% YoY, driven by enterprise renewals."), {}
    )
    assert verdict.passed is True, verdict.reasons
    assert record.findings == []


@pytest.mark.parametrize(
    "label,text",
    [
        ("refusal", "I'm sorry, as an AI language model I cannot help with that."),
        ("repeated filler", "the answer is " * 40),
        ("double-encoded json", '{"answer": "42", "sources": []}'),
    ],
)
def test_degraded_model_output_fails_the_gate_without_a_judge(label, text):
    """Signature registry only — no key, no model, no network."""
    verdict, _record, _rows = _run(_gen_pipeline(text), {})

    assert verdict.passed is False, f"{label} graded clean"
    assert "generate" in verdict.failing_nodes


@pytest.mark.parametrize("text", ["TODO", "N/A"])
def test_placeholder_answers_fail_the_gate(text):
    verdict, record, _rows = _run(_gen_pipeline(text), {})

    assert "generate" in _finding_nodes(record, "placeholder_detected"), "finding is raised"
    assert verdict.passed is False, "…but it does not gate"


def test_lorem_ipsum_is_detected():
    """A single occurrence, not just the repeated form RF-005 wanted."""
    verdict, record, _rows = _run(
        _gen_pipeline("Lorem ipsum dolor sit amet, consectetur adipiscing elit."), {}
    )
    assert verdict.passed is False or record.findings


# ── pipeline 6: crash, and who is blamed for it ──────────────────────────────


class HandoffState(TypedDict, total=False):
    order_id: str
    invoice_id: str
    receipt: str


def _crash_pipeline():
    g = StateGraph(HandoffState)
    g.add_node("load_order", lambda s: {"order_id": "ord-1"})
    g.add_node("bill", lambda s: {"notes": "billed, wrote the wrong key"})
    g.add_node("receipt", lambda s: {"receipt": s["invoice_id"].upper()})
    g.add_edge(START, "load_order")
    g.add_edge("load_order", "bill")
    g.add_edge("bill", "receipt")
    g.add_edge("receipt", END)
    return g.compile()


def test_a_declared_contract_blames_upstream_for_a_downstream_crash():
    """`bill` never wrote `invoice_id`; `receipt` dies on it."""
    verdict, record, _rows = _run(
        _crash_pipeline(), {}, {"invoice_id": ["receipt"]}, expect_raise=KeyError
    )

    assert verdict.passed is False
    assert record.overall_status == "crashed"
    upstream = _finding_nodes(record, "missing_field")
    assert upstream and upstream <= {"load_order", "bill"}, (
        "blame lands upstream of the crash site, not only on `receipt`"
    )


def test_a_keyerror_alone_is_enough_to_blame_upstream():
    """No consumer map. The KeyError names its own field — walk back on that."""
    verdict, record, _rows = _run(_crash_pipeline(), {}, expect_raise=KeyError)

    blamed = {f.node for f in record.findings}
    assert blamed - {"receipt"}, "somebody upstream of the crash is named"
    assert "bill" in verdict.failing_nodes, "the node that omitted invoice_id"
    assert record.root_cause_chain[0] == "bill", "the correlator must not eat the crash origin"
    assert record.first_failure_step == "bill", (
        "the omitter ran before the crash site, so it is the first failing step — "
        "naming `receipt` here puts the victim at the top of every report"
    )
    reason = next(f.reason for f in record.findings if f.type == "missing_field")
    assert "receipt" in reason, "the reason names the reader that crashed on it"


# ── pipeline 7: subgraph ─────────────────────────────────────────────────────


class NestedState(TypedDict, total=False):
    query: str
    docs: list[str]
    out: str


def _subgraph_app(inner_last):
    inner = StateGraph(NestedState)
    inner.add_node("normalize", lambda s: {"query": s.get("query", "").strip()})
    inner.add_node("retrieve", inner_last)
    inner.add_edge(START, "normalize")
    inner.add_edge("normalize", "retrieve")
    inner.add_edge("retrieve", END)

    outer = StateGraph(NestedState)
    outer.add_node("child", inner.compile())
    outer.add_node("render", lambda s: {"out": f"docs={s.get('docs')}"})
    outer.add_edge(START, "child")
    outer.add_edge("child", "render")
    outer.add_edge("render", END)
    return outer.compile()


def test_subgraph_steps_reach_the_ledger():
    """Capture works through a subgraph even where grading does not."""
    _verdict, _record, rows = _run(
        _subgraph_app(lambda s: {}), {"query": " contracts "}, {"docs": ["render"]}
    )

    assert "normalize" in rows and "retrieve" in rows, "inner nodes are recorded by name"
    assert rows["normalize"].update == {"query": "contracts"}
    assert rows["retrieve"].update == {}, "the inner no-op is faithfully on the notebook"


def test_a_silent_node_inside_a_subgraph_is_caught():
    """get_graph(xray=True) makes inner nodes first-class, successors and all."""
    verdict, _record, _rows = _run(
        _subgraph_app(lambda s: {}), {"query": " contracts "}, {"docs": ["render"]}
    )
    assert verdict.passed is False


# ── false positives: the reason this architecture exists ─────────────────────


class ScanState(TypedDict, total=False):
    target: str
    vulnerabilities: list[str]
    verdict: str


def test_a_legitimately_empty_result_set_is_not_a_silent_failure():
    """A clean security scan writes `vulnerabilities: []` and means it."""

    def scan(state: ScanState) -> dict:
        return {"vulnerabilities": []}

    def report(state: ScanState) -> dict:
        found = state.get("vulnerabilities") or []
        return {"verdict": "clean" if not found else f"{len(found)} issues"}

    g = StateGraph(ScanState)
    g.add_node("scan", scan)
    g.add_node("report", report)
    g.add_edge(START, "scan")
    g.add_edge("scan", "report")
    g.add_edge("report", END)

    verdict, record, _rows = _run(g.compile(), {"target": "repo"})

    assert verdict.passed is True, verdict.reasons
    assert record.findings == []


def test_an_empty_collection_only_fails_when_a_reader_declared_it():
    """The same `[]`, now under a declared contract, is a missing field.

    This is the line the architecture draws: an undeclared empty collection is
    a legitimate result ("no vulnerabilities"), while one a declared reader
    depends on is a silent failure. Emptiness alone is not the signal.
    """

    def scan(state: ScanState) -> dict:
        return {"vulnerabilities": []}

    def report(state: ScanState) -> dict:
        return {"verdict": f"{len(state.get('vulnerabilities') or [])} issues"}

    g = StateGraph(ScanState)
    g.add_node("scan", scan)
    g.add_node("report", report)
    g.add_edge(START, "scan")
    g.add_edge("scan", "report")
    g.add_edge("report", END)

    verdict, record, _rows = _run(g.compile(), {"target": "repo"}, {"vulnerabilities": ["report"]})

    assert verdict.passed is False
    assert "scan" in verdict.failing_nodes
    assert "report" not in verdict.failing_nodes
    assert any(f.field_path == "vulnerabilities" for f in record.findings)


def test_the_unchosen_branch_of_a_conditional_is_never_blamed():
    """A node that never ran is not a node that produced nothing."""

    class RouteState(TypedDict, total=False):
        amount: int
        decision: str
        result: str

    g = StateGraph(RouteState)
    g.add_node("assess", lambda s: {"decision": "auto" if s["amount"] < 100 else "manual"})
    g.add_node("auto_approve", lambda s: {"result": "approved"})
    g.add_node("manual_review", lambda s: {"result": "queued for a human"})
    g.add_edge(START, "assess")
    g.add_conditional_edges(
        "assess",
        lambda s: s["decision"],
        {"auto": "auto_approve", "manual": "manual_review"},
    )
    g.add_edge("auto_approve", END)
    g.add_edge("manual_review", END)

    verdict, _record, rows = _run(g.compile(), {"amount": 10}, {"result": ["auto_approve"]})

    assert verdict.passed is True, verdict.reasons
    assert "manual_review" not in rows, "the branch that never ran is not a ledger row"


def test_a_reader_that_produces_the_field_it_reads_is_not_a_failure():
    """An accumulator node reads and writes the same key."""

    class AccState(TypedDict, total=False):
        seed: int
        total: int
        out: str

    g = StateGraph(AccState)
    g.add_node("start", lambda s: {"seed": 2})
    g.add_node("accumulate", lambda s: {"total": s.get("total", 0) + s["seed"]})
    g.add_node("finish", lambda s: {"out": f"total={s['total']}"})
    g.add_edge(START, "start")
    g.add_edge("start", "accumulate")
    g.add_edge("accumulate", "finish")
    g.add_edge("finish", END)

    verdict, _record, _rows = _run(g.compile(), {}, {"total": ["accumulate"]})

    assert verdict.passed is True, verdict.reasons


def test_a_skinny_trace_is_refused_rather_than_graded_clean():
    """A sampled trace that drops the node spans must not report "no findings".

    The brief's rule: an incomplete recording is never a pass. Here the run
    boundary still arrives but every node span is sampled away — the shape a
    skinny OTel exporter produces.
    """
    from argus.recorder import IncompleteTraceError

    class SkinnyRecorder(ArgusRecorder):
        def on_chain_start(self, serialized, inputs, **kwargs):
            if (kwargs.get("metadata") or {}).get("langgraph_node"):
                return  # node span sampled out
            super().on_chain_start(serialized, inputs, **kwargs)

    g = StateGraph(CrmState)
    g.add_node("identify", lambda s: {"customer_id": "c"})
    g.add_edge(START, "identify")
    g.add_edge("identify", END)

    recorder = SkinnyRecorder(semantic_judge=False)
    bound = recorder.attach(g.compile())

    with pytest.raises(IncompleteTraceError):
        bound.invoke({"email": "a@b.c"})
