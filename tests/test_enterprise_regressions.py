"""Regressions surfaced by the enterprise-shaped pipeline stress suite.

Each test is the minimal graph that reproduced one defect found while running
six enterprise-style LangGraph pipelines (support desk, accounts payable, due
diligence, PR review bot, order fulfillment, multi-agent + RAG) through the
pivot path with the judge off. Same contract as the matrices: nothing patched,
every failing test names the node that must be blamed — and the healthy runs
must stay clean.

    D1   KeyError on a never-written top-level field blamed whoever wrote an
         unrelated dict (`account`) instead of the node that returned `{}`.
    D2   `I'm unable to` (contraction) and `[INSERT REPLY HERE]` had no
         signature; the uncontracted form and `[placeholder]` did.
    D4   `{"memo": "TBD"}` only warned; `{"answer": "TBD"}` failed. The verdict
         depended on what the author called the field.
    D5   The contextual layer checked only the *first* declared reader of a
         field; a field emptied between two readers passed.
    D6   No way to declare "must be present, may be empty".
    D7   `{"status": "declined"}` from a payment provider was clean.
    D8   No victim suppression: retrieve → rerank → generate each blamed for
         one empty upstream result.
    D9   A shared-cache signature (`^\\d+$`, warning) was promoted to critical
         on `answer`, and the cache path was cwd-relative.
    D10  The headline origin named a *passing* node carrying a warning while
         the failing node sat one step later.
"""

from __future__ import annotations

import json
from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

pytestmark = pytest.mark.integration


def _run(app, payload, consumers=None, *, expect_raise=None):
    recorder = ArgusRecorder(consumers=consumers, semantic_judge=False)
    bound = recorder.attach(app)
    if expect_raise is not None:
        with pytest.raises(expect_raise):
            bound.invoke(payload)
    else:
        bound.invoke(payload)
    record = load_run(recorder.session.run_id)
    return evaluate_run(record), record


def _blamed(record):
    return {f.node for f in record.findings if f.severity == "critical"}


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _linear(state_cls, nodes):
    g = StateGraph(state_cls)
    names = [n for n, _ in nodes]
    for n, fn in nodes:
        g.add_node(n, fn)
    g.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        g.add_edge(a, b)
    g.add_edge(names[-1], END)
    return g.compile()


# ── D1: KeyError on a never-written field blames the no-op, not the dict writer


class DeskState(TypedDict, total=False):
    ticket: str
    account: dict
    reply: str
    approved: bool


def test_d1_keyerror_on_never_written_field_blames_the_no_op_not_the_dict_writer():
    app = _linear(
        DeskState,
        [
            ("intake", lambda s: {"ticket": s["ticket"].strip()}),
            ("billing", lambda s: {"account": {"plan": "pro", "balance": 12.5}}),
            ("draft", lambda s: {}),  # forgot to write `reply`
            ("compliance", lambda s: {"approved": "refund" not in s["reply"]}),
        ],
    )
    _verdict, record = _run(app, {"ticket": " late invoice "}, expect_raise=KeyError)
    assert record.overall_status == "crashed"
    assert "draft" in _blamed(record), record.findings
    assert "billing" not in _blamed(record), "the dict writer is a bystander"
    assert record.root_cause_chain[0] == "draft"


# ── D2 / D4: vocabulary and field-name independence ─────────────────────────


class MemoState(TypedDict, total=False):
    ask: str
    memo: str
    published: bool


def _memo_app(text):
    return _linear(
        MemoState,
        [
            ("plan", lambda s: {"ask": s["ask"]}),
            ("draft_memo", lambda s: {"memo": text}),
            ("publish", lambda s: {"published": True}),
        ],
    )


@pytest.mark.parametrize(
    "text",
    [
        "TBD",  # D4: `memo` was not a deliverable key
        "[INSERT REPLY HERE]",  # D2: bracketed template variable
        "[Your Name]",
        "I'm unable to provide investment advice.",  # D2: contraction
    ],
)
def test_d2_d4_placeholder_or_refusal_memo_is_blamed(text):
    verdict, record = _run(_memo_app(text), {"ask": "diligence on ACME"})
    assert verdict.passed is False, record.findings
    assert _blamed(record) == {"draft_memo"}, record.findings


@pytest.mark.parametrize(
    "text",
    [
        "ACME's Q3 revenue grew 12% year over year; churn held at 2.1%.",
        "Cannot recommend at this valuation; see the risk table below.",  # prose, not a refusal
    ],
)
def test_d2_d4_real_memo_stays_clean(text):
    verdict, record = _run(_memo_app(text), {"ask": "diligence on ACME"})
    assert verdict.passed is True, record.findings


def test_d4_user_text_field_is_not_a_deliverable():
    """`text` holds the customer's words. 'I cannot reset my password' is a ticket."""

    class T(TypedDict, total=False):
        text: str
        reply: str

    app = _linear(
        T,
        [
            ("intake", lambda s: {"text": s["text"].strip()}),
            ("draft", lambda s: {"reply": "Use the reset link in your email."}),
        ],
    )
    verdict, record = _run(app, {"text": "I cannot reset my password"})
    assert verdict.passed is True, record.findings


# ── D5 / D6 / D8: contextual layer ───────────────────────────────────────────


class OrderState(TypedDict, total=False):
    order_id: str
    line_items: list
    total: float
    shipped: bool


ORDER_CONSUMERS = {"line_items": ["price", "ship"]}


def _order_app(price):
    return _linear(
        OrderState,
        [
            ("load", lambda s: {"line_items": [{"sku": "A", "qty": 2, "unit": 5.0}]}),
            ("price", price),
            ("ship", lambda s: {"shipped": True}),
        ],
    )


def test_d5_field_emptied_between_two_readers_blames_the_dropper():
    app = _order_app(lambda s: {"total": 10.0, "line_items": []})
    verdict, record = _run(app, {"order_id": "o1"}, ORDER_CONSUMERS)
    assert verdict.passed is False
    assert _blamed(record) == {"price"}, record.findings


def test_d5_healthy_two_reader_order_is_clean():
    app = _order_app(lambda s: {"total": sum(i["qty"] * i["unit"] for i in s["line_items"])})
    verdict, record = _run(app, {"order_id": "o1"}, ORDER_CONSUMERS)
    assert verdict.passed is True, record.findings


class ReviewState(TypedDict, total=False):
    diff: str
    issues: list
    summary: str


def _review_app(aggregate):
    return _linear(
        ReviewState,
        [
            ("fetch", lambda s: {"diff": "+typo fix"}),
            ("collect", lambda s: {"issues": []}),  # LGTM path
            ("aggregate", aggregate),
            ("summarize", lambda s: {"summary": f"{len(s.get('issues') or [])} issues"}),
        ],
    )


def test_d6_plain_declaration_means_non_empty():
    verdict, record = _run(_review_app(lambda s: {"summary": ""}), {}, {"issues": ["summarize"]})
    assert verdict.passed is False
    assert "collect" in _blamed(record)


def test_d6_allow_empty_declares_presence_only():
    spec = {"issues": {"readers": ["summarize"], "allow_empty": True}}
    verdict, record = _run(_review_app(lambda s: {"summary": "LGTM, one typo fixed."}), {}, spec)
    assert verdict.passed is True, record.findings

    verdict, record = _run(_review_app(lambda s: {"issues": None}), {}, spec)
    assert verdict.passed is False
    assert _blamed(record) == {"aggregate"}, record.findings


class RagState(TypedDict, total=False):
    question: str
    docs: list
    ranked: list
    citations: list
    answer: str


RAG_CONSUMERS = {"docs": ["rerank"], "ranked": ["generate"], "citations": ["answer"]}


def test_d8_one_empty_retrieval_blames_retrieve_alone():
    app = _linear(
        RagState,
        [
            ("retrieve", lambda s: {"docs": []}),
            ("rerank", lambda s: {"ranked": s["docs"][:3]}),
            ("generate", lambda s: {"citations": [d["id"] for d in s["ranked"]]}),
            ("answer", lambda s: {"answer": "See sources: " + ", ".join(s["citations"])}),
        ],
    )
    verdict, record = _run(app, {"question": "q"}, RAG_CONSUMERS)
    assert verdict.passed is False
    assert "retrieve" in _blamed(record), record.findings
    assert not {"rerank", "generate", "answer"} & _blamed(record), record.findings


def test_d8_filter_over_a_missing_field_is_a_victim_not_a_producer():
    """`validate` reads `line_items`, has nothing to read, writes `[]`. Declared as
    a reader it is starved; the never-written rule then walks back upstream."""

    class Ap(TypedDict, total=False):
        raw: str
        line_items: list
        total: float

    app = _linear(
        Ap,
        [
            ("ocr", lambda s: {"raw": json.dumps({"line_items": [{"amt": 5}]})}),
            ("validate", lambda s: {"line_items": [i for i in s.get("line_items", []) if i]}),
            ("totals", lambda s: {"total": sum(i["amt"] for i in s["line_items"])}),
        ],
    )
    verdict, record = _run(app, {}, {"line_items": ["validate", "totals"]})
    assert verdict.passed is False
    assert "ocr" in _blamed(record), record.findings
    assert "validate" not in _blamed(record), record.findings


def test_d8_accumulator_that_produces_a_value_is_still_exempt():
    class Acc(TypedDict, total=False):
        seed: int
        total: int

    app = _linear(
        Acc,
        [
            ("start", lambda s: {"seed": 2}),
            ("accumulate", lambda s: {"total": s.get("total", 0) + s["seed"]}),
        ],
    )
    verdict, record = _run(app, {}, {"total": ["accumulate"]})
    assert verdict.passed is True, record.findings


# ── D7: failure words in status fields ───────────────────────────────────────


class PayState(TypedDict, total=False):
    order_id: str
    charge: dict
    label: str


def _pay_app(charge_tool):
    def charge(s):
        return {"charge": charge_tool.invoke({"order_id": s["order_id"]})}

    return _linear(
        PayState,
        [("charge", charge), ("ship", lambda s: {"label": "1Z999"})],
    )


def test_d7_declined_payment_is_blamed_on_charge():
    @tool
    def psp(order_id: str) -> dict:
        """Charge the card."""
        return {"id": "ch_1", "status": "declined", "decline_code": "insufficient_funds"}

    verdict, record = _run(_pay_app(psp), {"order_id": "o1"})
    assert verdict.passed is False
    assert _blamed(record) == {"charge"}, record.findings


@pytest.mark.parametrize("status", ["succeeded", "cancelled", "pending", "requires_action"])
def test_d7_non_failure_status_words_are_clean(status):
    @tool
    def psp(order_id: str) -> dict:
        """Charge the card."""
        return {"id": "ch_1", "status": status}

    verdict, record = _run(_pay_app(psp), {"order_id": "o1"})
    assert verdict.passed is True, record.findings


# ── D9: shared signatures are not promoted; cache path is project-rooted ────


def test_d9_shared_signature_is_not_promoted_to_critical(monkeypatch, tmp_path):
    from argus import registry

    shared = tmp_path / ".argus"
    shared.mkdir(exist_ok=True)
    (shared / "shared_signatures_cache.json").write_text(
        json.dumps(
            [
                {
                    "id": "SH-TEST01",
                    "category": "malformed_payload",
                    "pattern": "^\\d+$",
                    "match_strategy": "regex",
                    "severity": "warning",
                    "description": "numeric instead of JSON",
                    "source": "shared",
                }
            ]
        )
    )
    monkeypatch.setenv("ARGUS_DIR", str(tmp_path))
    registry.reload_registry()
    try:
        assert any(s["id"] == "SH-TEST01" for s in registry.get_registry()), (
            "cache is resolved through ARGUS_DIR, not cwd"
        )

        class Q(TypedDict, total=False):
            question: str
            answer: str

        app = _linear(
            Q,
            [
                ("ask", lambda s: {"question": s["question"]}),
                ("solve", lambda s: {"answer": "60"}),
            ],
        )
        verdict, record = _run(app, {"question": "12 * 5?"})
        assert verdict.passed is True, record.findings
    finally:
        monkeypatch.delenv("ARGUS_DIR")
        registry.reload_registry()


def test_d9_bundled_placeholder_is_still_promoted():
    class Q(TypedDict, total=False):
        question: str
        answer: str

    app = _linear(
        Q,
        [("ask", lambda s: {"question": s["question"]}), ("solve", lambda s: {"answer": "N/A"})],
    )
    verdict, record = _run(app, {"question": "12 * 5?"})
    assert verdict.passed is False
    assert _blamed(record) == {"solve"}


# ── D10: the headline names a failing node ───────────────────────────────────


def test_d10_headline_origin_is_the_failing_node_not_a_warned_bystander():
    """`retrieve` returns one thin doc (warning-level), `rerank` empties it."""
    app = _linear(
        RagState,
        [
            ("retrieve", lambda s: {"docs": [{"id": "d1", "text": "ok"}]}),
            ("rerank", lambda s: {"ranked": []}),
            ("generate", lambda s: {"answer": "I could not find relevant sources."}),
        ],
    )
    verdict, record = _run(app, {"question": "q"}, {"ranked": ["generate"]})
    assert verdict.passed is False
    failing = {s.node_name for s in record.steps if s.status != "pass"}
    assert record.first_failure_step in failing, (record.first_failure_step, failing)
    assert record.root_cause_chain and set(record.root_cause_chain) <= failing, (
        record.root_cause_chain,
        failing,
    )
    assert "rerank" in record.root_cause_chain


# ── E8 / #135: own-router KeyError blames the node, not the previous writer ──


class KycState(TypedDict, total=False):
    risk: float
    decision: dict
    notes: str


def test_e8_own_router_keyerror_blames_decide_not_aggregate_risk():
    """A node returns `{}`; its own conditional edge then reads the missing field.

    LangGraph reports the KeyError on `decide`. crash_origins must not walk
    back to `aggregate_risk` (it wrote an unrelated `risk`).
    """

    def decide(state):
        return {}

    def route(state):
        return "open_account" if state["decision"]["outcome"] == "approve" else "manual_review"

    g = StateGraph(KycState)
    g.add_node("aggregate_risk", lambda s: {"risk": 0.9})
    g.add_node("decide", decide)
    g.add_node("open_account", lambda s: {"notes": "opened"})
    g.add_node("manual_review", lambda s: {"notes": "review"})
    g.add_edge(START, "aggregate_risk")
    g.add_edge("aggregate_risk", "decide")
    g.add_conditional_edges("decide", route, ["open_account", "manual_review"])
    g.add_edge("open_account", END)
    g.add_edge("manual_review", END)

    _verdict, record = _run(g.compile(), {}, expect_raise=KeyError)
    assert record.overall_status == "crashed"
    assert "decide" in _blamed(record), record.findings
    assert "aggregate_risk" not in _blamed(record), record.findings
    assert record.root_cause_chain[0] == "decide"
    assert record.first_failure_step == "decide"
