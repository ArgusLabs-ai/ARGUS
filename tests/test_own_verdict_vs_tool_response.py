"""E1 / E9: a node's own verdict is not a tool response.

The error-key, success-boolean and status-word rules were written for tool
payloads — a payment API returning `{"status": "declined"}`, a warehouse
returning `{"error": "permission denied"}`. They ran on every node's *own*
output too, where the same shapes are the node's work product:

    E1  {"decision": {"status": "denied", ...}}      a claim correctly denied
    E9  {"lint": {"ok": False, "errors": [...]}}     a linter correctly reporting

Both failed CI. Every claims / lending / KYC / moderation pipeline fails on its
normal "no" path, and every linter / guardrail / critic node is blamed for
doing its job — beside the node that actually produced the bad input.

The fat trace records tool I/O separately (`NodeEvent.tool_calls`), so the real
cases are still caught there. On a node's own output these three rules are now
warnings: visible in `argus show`, not a gate, and the soft flag the ambiguous
tier (#130) will review. Numeric HTTP status stays critical either way — no
business decision is "500".
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.inspector import inspect_tool_calls, inspect_tool_outputs
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _sev(result, field: str) -> str | None:
    for tf in result:
        if tf.field_name == field:
            return tf.severity
    return None


# ── unit: the same shape, graded by where it came from ───────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,field",
    [
        ({"decision": {"status": "denied", "rationale": "Peril not covered."}}, "decision.status"),
        ({"payment": {"status": "declined"}}, "payment.status"),
        ({"lint": {"errors": ["syntax error at or near SELEC"]}}, "lint.errors"),
        ({"review": {"ok": False, "issues": ["missing test"]}}, "review.ok"),
    ],
)
def test_a_nodes_own_verdict_is_a_warning(payload, field):
    own = inspect_tool_outputs(payload, own_output=True).tool_failures
    assert _sev(own, field) == "warning", [(t.field_name, t.severity) for t in own]


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,field",
    [
        # The node saying *it* broke — the swallowed failure ARGUS exists for.
        ({"error": "API timeout"}, "error"),
        ({"error": True, "result": ""}, "error"),
        ({"analysis": "", "error": "out of memory"}, "error"),
        # A stored tool result with no findings beside it is not a verdict.
        (
            {"tool_results": [{"success": False, "message": "Auth failed"}]},
            "tool_results[0].success",
        ),
        ({"payment": {"success": False}}, "payment.success"),
    ],
)
def test_a_nodes_own_failure_is_still_critical(payload, field):
    """The narrow line: `errors: [...]` is a report, `error: "..."` is breakage."""
    own = inspect_tool_outputs(payload, own_output=True).tool_failures
    assert _sev(own, field) == "critical", [(t.field_name, t.severity) for t in own]


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,field",
    [
        ({"status": "declined", "decline_code": "insufficient_funds"}, "psp.status"),
        ({"error": "permission denied for relation orders"}, "psp.error"),
        ({"success": False}, "psp.success"),
    ],
)
def test_the_same_shape_from_a_tool_is_still_critical(payload, field):
    found = inspect_tool_calls([{"name": "psp", "output": payload}])
    assert _sev(found, field) == "critical", [(t.field_name, t.severity) for t in found]


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,field",
    [
        ({"status_code": 404}, "status_code"),
        ({"resp": {"status": 500}}, "resp.status"),
    ],
)
def test_numeric_http_status_stays_critical_on_a_nodes_own_output(payload, field):
    own = inspect_tool_outputs(payload, own_output=True).tool_failures
    assert _sev(own, field) == "critical", [(t.field_name, t.severity) for t in own]


@pytest.mark.unit
def test_the_default_is_unchanged_for_direct_callers():
    """`own_output` defaults off, so nothing that calls the scan directly is weakened."""
    found = inspect_tool_outputs({"decision": {"status": "denied"}}).tool_failures
    assert _sev(found, "decision.status") == "critical"


# ── pipeline: E1 and E9 end to end ───────────────────────────────────────────


class _Claim(TypedDict, total=False):
    peril: str
    coverage: dict
    decision: dict
    letter: str


def _claims_app(covered: bool):
    def coverage_check(s):
        return {"coverage": {"covered": covered, "deductible": 500.0}}

    def adjudicate(s):
        if not s["coverage"]["covered"]:
            return {
                "decision": {
                    "status": "denied",
                    "amount": 0.0,
                    "rationale": "Peril not covered by policy.",
                }
            }
        return {"decision": {"status": "approved", "amount": 2400.0, "rationale": "Covered."}}

    def letter(s):
        return {"letter": f"Your claim was {s['decision']['status']}."}

    g = StateGraph(_Claim)
    g.add_node("coverage_check", coverage_check)
    g.add_node("adjudicate", adjudicate)
    g.add_node("letter", letter)
    g.add_edge(START, "coverage_check")
    g.add_edge("coverage_check", "adjudicate")
    g.add_edge("adjudicate", "letter")
    g.add_edge("letter", END)
    return g.compile()


def _grade(app, payload, consumers=None):
    recorder = ArgusRecorder(semantic_judge=False, consumers=consumers)
    recorder.attach(app).invoke(payload)
    record = load_run(recorder.session.run_id)
    return evaluate_run(record), record


@pytest.mark.integration
@pytest.mark.parametrize("covered", [True, False], ids=["approved", "denied"])
def test_a_claims_pipeline_is_clean_on_both_decisions(covered):
    """E1: the correct "no" path is not a failure."""
    verdict, _ = _grade(_claims_app(covered), {"peril": "flood"})
    assert verdict.passed, verdict


class _Sql(TypedDict, total=False):
    question: str
    sql: str
    lint: dict
    rows: list


def _sql_app(bad_sql: bool):
    @tool
    def warehouse_query(sql: str) -> dict:
        """Run SQL on the warehouse."""
        return {"rows": [{"month": "2024-07", "revenue": 401220}]}

    def write_sql(s):
        return {"sql": "" if bad_sql else "SELECT month, revenue FROM orders"}

    def lint_sql(s):
        ok = s["sql"].strip().upper().startswith("SELECT")
        return {"lint": {"ok": ok, "errors": [] if ok else ["empty statement"]}}

    def run_sql(s):
        return {"rows": warehouse_query.invoke({"sql": s["sql"]})["rows"]}

    g = StateGraph(_Sql)
    g.add_node("write_sql", write_sql)
    g.add_node("lint_sql", lint_sql)
    g.add_node("run_sql", run_sql)
    g.add_edge(START, "write_sql")
    g.add_edge("write_sql", "lint_sql")
    g.add_edge("lint_sql", "run_sql")
    g.add_edge("run_sql", END)
    return g.compile()


@pytest.mark.integration
def test_a_linter_reporting_errors_is_not_blamed():
    """E9: the writer emitted empty SQL — the linter saying so is not a second failure."""
    verdict, _ = _grade(_sql_app(bad_sql=True), {"question": "q"}, consumers={"sql": ["run_sql"]})
    assert not verdict.passed, verdict
    assert verdict.failing_nodes == ("write_sql",), verdict


@pytest.mark.integration
def test_a_healthy_sql_pipeline_with_a_linter_is_clean():
    verdict, _ = _grade(_sql_app(bad_sql=False), {"question": "q"}, consumers={"sql": ["run_sql"]})
    assert verdict.passed, verdict


@pytest.mark.integration
def test_a_swallowed_tool_failure_is_still_caught():
    """The guard on the other side: a real tool failure the node stored anyway."""

    @tool
    def charge_card(amount: float) -> dict:
        """Charge the customer's card."""
        return {"status": "declined", "decline_code": "insufficient_funds"}

    class S(TypedDict, total=False):
        amount: float
        payment: dict
        shipped: bool

    g = StateGraph(S)
    g.add_node("charge", lambda s: {"payment": charge_card.invoke({"amount": 49.99})})
    g.add_node("ship", lambda s: {"shipped": True})
    g.add_edge(START, "charge")
    g.add_edge("charge", "ship")
    g.add_edge("ship", END)

    verdict, _ = _grade(g.compile(), {"amount": 49.99})
    assert not verdict.passed, verdict
    assert verdict.failing_nodes == ("charge",), verdict
