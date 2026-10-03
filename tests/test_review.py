"""The run reviewer (`argus.review`): two independent checks must agree on a heuristic.

Real LangGraph graphs, the real recorder, a scripted chat model so the trace
carries model calls (the text rules only read model nodes), and the reviewer's
LLM transport stubbed. Every test names the step that is blamed, or that the
run is clean.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from argus import ArgusRecorder
from argus.check import evaluate_run
from argus.review import _decide
from argus.storage import load_run

pytestmark = pytest.mark.integration

PURPOSES = {
    "classify": "Reads the ticket and extracts the order id.",
    "act": "Calls the refund tool when the order is eligible.",
    "draft_reply": "Writes the customer reply from what earlier steps did.",
}


class _Model(BaseChatModel):
    text: str

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        meta = {"finish_reason": "stop", "model_name": "gpt-4o-mini"}
        msg = AIMessage(content=self.text, response_metadata=meta)
        return ChatResult(generations=[ChatGeneration(message=msg, generation_info=meta)])


class _S(TypedDict, total=False):
    ticket: str
    order_id: str
    refund_id: str
    exists: bool
    reply: str


def _graph(
    reply: str, *, refund: bool = False, act_returns: dict | None = None, tool_out: Any = None
):
    @tool
    def create_refund(order_id: str) -> dict:
        """Refund an order."""
        return {"id": "re_1", "status": "succeeded"}

    @tool
    def get_report(day: str) -> Any:
        """Does today's report exist?"""
        return tool_out

    def classify(s: _S) -> dict:
        return {"order_id": "A-1001"}

    def act(s: _S) -> dict:
        if act_returns is not None:
            return act_returns
        if tool_out is not None:
            return {"exists": bool(get_report.invoke({"day": "today"}).get("exists"))}
        if refund:
            return {"refund_id": create_refund.invoke({"order_id": s["order_id"]})["id"]}
        return {"refund_id": "none"}

    def draft_reply(s: _S) -> dict:
        return {"reply": _Model(text=reply).invoke([HumanMessage("reply")]).content}

    g = StateGraph(_S)
    for fn in (classify, act, draft_reply):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "classify")
    g.add_edge("classify", "act")
    g.add_edge("act", "draft_reply")
    g.add_edge("draft_reply", END)
    return g.compile()


def _stub(
    monkeypatch,
    *,
    report=(),
    real=True,
    correction="the exact fix",
    arithmetic=None,
    boom=False,
    second=False,
    second_boom=False,
):
    """The reviewer's transport. ``second`` is what the second model (o4-mini) says."""
    calls = {"checker": 0, "verifier": 0, "second": 0}

    def fake(model, system, user, max_tokens):
        if boom:
            raise RuntimeError("provider down")
        if system.startswith("You verify ONE run"):
            calls["checker"] += 1
            return {
                "failures": [
                    {
                        "node": n,
                        "kind": "contradicted",
                        "claim": "x",
                        "evidence": "y",
                        "why": f"{n} contradicts its evidence",
                        "confidence": 0.9,
                    }
                    for n in report
                ]
            }
        if model == "o4-mini":
            calls["second"] += 1
            if second_boom:
                raise RuntimeError("o4-mini not available")
            return {"real_defect": second, "correct_value": correction if second else None}
        calls["verifier"] += 1
        return {"real_defect": real, "correct_value": correction, "arithmetic": arithmetic}

    monkeypatch.setattr("argus.review._complete", fake)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    return calls


def _grade(app, **kw):
    rec = ArgusRecorder(purposes=PURPOSES, **kw)
    rec.attach(app).invoke({"ticket": "refund A-1001 please"})
    record = load_run(rec.session.run_id)
    return record, evaluate_run(record)


def _types(record, severity):
    return {(f.node, f.type) for f in record.findings if f.severity == severity}


# ── heuristic hits need the reviewer ─────────────────────────────────────────

FALSE_CLAIM = "Good news - I've processed your refund for order A-1001."


def test_unconfirmed_heuristic_hit_is_a_warning(monkeypatch):
    _stub(monkeypatch, report=())
    record, verdict = _grade(_graph(FALSE_CLAIM))
    assert verdict.passed, verdict.reasons
    assert ("draft_reply", "unperformed_action") in _types(record, "warning")


def test_confirmed_heuristic_hit_fails_on_its_node(monkeypatch):
    _stub(monkeypatch, report=["draft_reply"])
    record, verdict = _grade(_graph(FALSE_CLAIM))
    assert not verdict.passed
    assert verdict.failing_nodes == ("draft_reply",)
    step = next(e for e in record.steps if e.node_name == "draft_reply")
    assert step.review and step.review[0]["role"] == "confirms"


def test_a_healthy_refund_confirmation_is_not_a_claim_without_a_tool(monkeypatch):
    """The blind-probe false alarm: "I've approved a refund" after create_refund ran."""
    _stub(monkeypatch, report=())
    reply = "Hi Dana, I've approved a refund for order A-1001."
    record, verdict = _grade(_graph(reply, refund=True), review=False, semantic_judge=False)
    assert verdict.passed, verdict.reasons


def test_rules_alone_still_fail_a_false_claim(monkeypatch):
    """With no reviewer the heuristic stands — today's deterministic gate."""
    _stub(monkeypatch, report=())
    record, verdict = _grade(_graph(FALSE_CLAIM), review=False, semantic_judge=False)
    assert verdict.failing_nodes == ("draft_reply",)
    assert ("draft_reply", "unperformed_action") in _types(record, "critical")


def test_a_reviewer_outage_leaves_the_rules_in_charge(monkeypatch):
    _stub(monkeypatch, boom=True)
    _record, verdict = _grade(_graph(FALSE_CLAIM))
    assert verdict.failing_nodes == ("draft_reply",)


def test_a_verdict_without_a_concrete_correction_does_not_confirm(monkeypatch):
    calls = _stub(monkeypatch, report=["draft_reply"], correction=None)
    _record, verdict = _grade(_graph(FALSE_CLAIM))
    assert calls == {"checker": 1, "verifier": 1, "second": 0}
    assert verdict.passed, verdict.reasons


# ── strict evidence never waits for the reviewer ─────────────────────────────


def test_a_strict_failure_stands_whatever_the_reviewer_says(monkeypatch):
    _stub(monkeypatch, report=())
    _record, verdict = _grade(_graph("Done.", act_returns={}))
    assert verdict.failing_nodes == ("act",)


def test_a_404_existence_check_is_settled_by_the_reviewer(monkeypatch):
    _stub(monkeypatch, report=())
    _record, verdict = _grade(
        _graph("Nothing posted yet today.", tool_out={"status": 404, "message": "Not Found"})
    )
    assert verdict.passed, verdict.reasons


def test_a_500_is_strict(monkeypatch):
    _stub(monkeypatch, report=())
    _record, verdict = _grade(
        _graph("Done.", tool_out={"status": 500, "message": "Internal Server Error"})
    )
    assert verdict.failing_nodes == ("act",)


# ── a rule warning the reviewer verifies becomes a fail ──────────────────────

TEMPLATE = "Hi {customer_name}, your refund for order {order_id} is on its way."


def test_a_warning_the_reviewer_verifies_fails(monkeypatch):
    _stub(monkeypatch, report=["draft_reply"])
    record, verdict = _grade(_graph(TEMPLATE, refund=True))
    assert verdict.failing_nodes == ("draft_reply",)
    assert ("draft_reply", "review_confirmed") in _types(record, "critical")
    step = next(e for e in record.steps if e.node_name == "draft_reply")
    assert step.review[0]["role"] == "promoted"


def test_the_same_warning_alone_does_not_fail(monkeypatch):
    _stub(monkeypatch, report=())
    record, verdict = _grade(_graph(TEMPLATE, refund=True))
    assert verdict.passed, verdict.reasons
    assert any(f.node == "draft_reply" and f.severity == "warning" for f in record.findings)


def test_a_verified_finding_with_no_rule_signal_is_advisory(monkeypatch):
    _stub(monkeypatch, report=["classify"])
    record, verdict = _grade(
        _graph("Hi Dana, I've approved a refund for order A-1001.", refund=True)
    )
    assert verdict.passed, verdict.reasons
    advisory = [f for f in record.findings if f.node == "classify" and f.source == "llm"]
    assert advisory and advisory[0].severity == "warning"
    assert "advisory" in advisory[0].reason


# ── the scan goes on past a hit the reviewer rejected ────────────────────────


def test_scan_continues_past_an_unconfirmed_heuristic(monkeypatch):
    """`classify` (a model node) states a number nothing supports — D12, heuristic.
    Unconfirmed, it must not stop the whole-trace scan before `draft_reply`,
    whose `{{var}}` template (D8) is strict."""
    _stub(monkeypatch, report=())

    class S(TypedDict, total=False):
        ticket: str
        note: str
        reply: str

    def classify(s: S) -> dict:
        return {
            "note": _Model(text="Customer has 7 open orders").invoke([HumanMessage("x")]).content
        }

    def draft_reply(s: S) -> dict:
        return {
            "reply": _Model(text="Dear {{customer_name}}, thanks.")
            .invoke([HumanMessage("x")])
            .content
        }

    g = StateGraph(S)
    g.add_node("classify", classify)
    g.add_node("draft_reply", draft_reply)
    g.add_edge(START, "classify")
    g.add_edge("classify", "draft_reply")
    g.add_edge("draft_reply", END)
    _record, verdict = _grade(g.compile())
    assert verdict.failing_nodes == ("draft_reply",)


# ── switching it on ──────────────────────────────────────────────────────────


def test_review_true_needs_purposes():
    with pytest.raises(ValueError, match="purposes"):
        ArgusRecorder(review=True)


def test_purposes_can_come_from_the_baseline_file(monkeypatch):
    calls = _stub(monkeypatch, report=())
    rec = ArgusRecorder(baseline={"version": 1, "nodes": {}, "purposes": PURPOSES})
    rec.attach(_graph(FALSE_CLAIM)).invoke({"ticket": "x"})
    assert calls["checker"] == 1
    assert evaluate_run(load_run(rec.session.run_id)).passed


def test_no_purposes_no_reviewer(monkeypatch):
    calls = _stub(monkeypatch, report=())
    rec = ArgusRecorder(semantic_judge=False)
    rec.attach(_graph(FALSE_CLAIM)).invoke({"ticket": "x"})
    assert calls["checker"] == 0
    assert evaluate_run(load_run(rec.session.run_id)).failing_nodes == ("draft_reply",)


# ── arithmetic is settled by code ────────────────────────────────────────────


@pytest.mark.unit
def test_code_decides_arithmetic_whatever_the_verdict():
    wrong = {
        "real_defect": False,
        "arithmetic": {"expression": "94.99 - 0", "stated_value": 49.99},
    }
    right = {
        "real_defect": True,
        "correct_value": "x",
        "arithmetic": {"expression": "40 + 2", "stated_value": 42},
    }
    assert _decide(wrong)[0] is True
    assert _decide(right)[0] is False


@pytest.mark.unit
def test_a_formula_that_is_not_arithmetic_falls_back_to_the_verdict():
    v = {
        "real_defect": True,
        "correct_value": "3",
        "arithmetic": {"expression": "__import__('os')", "stated_value": 1},
    }
    assert _decide(v) == (True, "3")
    assert _decide({**v, "arithmetic": {"expression": "9**9**9", "stated_value": 1}}) == (
        True,
        "3",
    )


# ── the "never written" guess ────────────────────────────────────────────────


def _skipped_payment_graph():
    class S(TypedDict, total=False):
        request: str
        payment: dict
        itinerary: str

    def supervisor(s: S) -> dict:
        return {"request": "SFO-BER"}  # routes straight past `pay`: payment never written

    def itinerary(s: S) -> dict:
        total = (s.get("payment") or {}).get("total", 0)
        return {"itinerary": f"You're booked! Total charged: ${total:,.2f}."}

    g = StateGraph(S)
    g.add_node("supervisor", supervisor)
    g.add_node("itinerary", itinerary)
    g.add_edge(START, "supervisor")
    g.add_edge("supervisor", "itinerary")
    g.add_edge("itinerary", END)
    return g.compile()


def test_a_never_written_guess_is_confirmed_by_its_starved_reader(monkeypatch):
    """ship_eval travel: the reviewer flagged `itinerary` ("Total charged: $0.00"),
    not the supervisor the guess blames. The starved reader is the corroboration."""
    _stub(monkeypatch, report=["itinerary"])
    rec = ArgusRecorder(purposes=PURPOSES, consumers={"payment": ["itinerary"]})
    rec.attach(_skipped_payment_graph()).invoke({})
    verdict = evaluate_run(load_run(rec.session.run_id))
    assert "supervisor" in verdict.failing_nodes


def test_an_unconfirmed_never_written_guess_is_a_warning(monkeypatch):
    _stub(monkeypatch, report=())
    rec = ArgusRecorder(purposes=PURPOSES, consumers={"payment": ["itinerary"]})
    rec.attach(_skipped_payment_graph()).invoke({})
    record = load_run(rec.session.run_id)
    assert evaluate_run(record).passed
    assert ("supervisor", "missing_field_guess") in _types(record, "warning")


# ── two models, no rule ──────────────────────────────────────────────────────


HEALTHY_REPLY = "Hi Dana, I've approved a refund for order A-1001."


def test_two_models_verifying_a_finding_no_rule_saw_fails_ci(monkeypatch):
    """Final check: paying 10x over the PO or the wrong vendor's account has no rule
    signal. Two different models verifying it is the bar for failing on it."""
    calls = _stub(monkeypatch, report=["classify"], second=True)
    record, verdict = _grade(_graph(HEALTHY_REPLY, refund=True))
    assert verdict.failing_nodes == ("classify",)
    assert ("classify", "review_verified") in _types(record, "critical")
    step = next(e for e in record.steps if e.node_name == "classify")
    assert step.review[0]["role"] == "two_models"
    assert calls["second"] == 1


def test_one_model_alone_stays_advisory(monkeypatch):
    _stub(monkeypatch, report=["classify"], second=False)
    _record, verdict = _grade(_graph(HEALTHY_REPLY, refund=True))
    assert verdict.passed, verdict.reasons


def test_a_second_model_outage_is_advisory_not_a_lost_review(monkeypatch):
    """The second opinion can only add a fail; losing it must not drop the rules'
    verdict or the first verifier's confirmations."""
    _stub(monkeypatch, report=["classify", "draft_reply"], second_boom=True)
    record, verdict = _grade(_graph(FALSE_CLAIM))
    assert verdict.failing_nodes == ("draft_reply",)  # D14 + first verifier still confirm
    assert not any(f.type == "review_verified" for f in record.findings)


def test_the_second_model_is_only_asked_when_the_first_verified(monkeypatch):
    calls = _stub(monkeypatch, report=["classify"], real=False, second=True)
    _record, verdict = _grade(_graph(HEALTHY_REPLY, refund=True))
    assert verdict.passed
    assert calls["second"] == 0


@pytest.mark.unit
def test_argus_show_prints_what_the_reviewer_verified():
    """#149: an advisory note nobody can see is no note at all."""
    from conftest import make_event, make_run_record
    from typer.testing import CliRunner

    from argus.cli.main import app
    from argus.storage import save_run

    event = make_event(node_name="validate")
    event.review = [
        {
            "kind": "wrong_decision",
            "claim": "approved",
            "why": "invoice is 10x the PO total",
            "correction": "hold the invoice",
            "role": "advisory",
        }
    ]
    save_run(make_run_record(events=[event], status="clean", run_id="20260101-000001-rev00001"))
    result = CliRunner().invoke(app, ["show", "20260101-000001-rev00001"])
    assert result.exit_code == 0, result.output
    assert "Run reviewer" in result.output
    assert "invoice is 10x the PO total" in result.output
    assert "advisory, not gating" in result.output
