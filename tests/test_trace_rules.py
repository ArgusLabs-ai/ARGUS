"""The trace rules (`argus.trace_rules`): one positive and one negative case each.

Each rule reads the finished fat trace and marks the step that caused a silent
failure. Measured on a 255-fault taxonomy suite, they lift coverage from 51% to
~92% with no false positive on 63 healthy runs (54 of them real model prose).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from argus import trace_rules as R
from argus.models import LLMCallInfo, LLMUsage

pytestmark = pytest.mark.unit


@dataclass
class Ev:
    node_name: str
    input_state: dict = field(default_factory=dict)
    output_dict: Any = field(default_factory=dict)
    tool_calls: list = field(default_factory=list)
    llm_usage: Any = None
    status: str = "pass"
    step_index: int = 0
    inspection: Any = None


def _llm(text: str = "ok", finish: str = "stop") -> LLMUsage:
    return LLMUsage(
        calls=[LLMCallInfo("gpt-4o", 10, 5, 15, finish_reason=finish, output_text=text)]
    )


def _kinds(hits):
    return [(h.event.node_name, h.failure_type) for h in hits]


def _run(events, **kw):
    for i, e in enumerate(events):
        e.step_index = i
    return R.run_rules(events, **kw)


# D1 ─────────────────────────────────────────────────────────────────────────


def test_d1_a_key_the_state_does_not_have_is_blamed_on_its_writer():
    ev = [Ev("retrieve", output_dict={"dosc": [1]})]
    assert _kinds(_run(ev, state_keys=["docs", "q"], consumers={"docs": ["answer"]})) == [
        ("retrieve", "unknown_state_key")
    ]


def test_d1_needs_the_meant_key_to_be_declared():
    """`noise_a` beside a never-written `noise_b`: sibling naming, not proof of a typo."""
    assert _run([Ev("A", output_dict={"noise_a": 1})], state_keys=["noise_b"]) == []


def test_d1_quiet_without_a_schema_and_for_known_keys():
    assert _run([Ev("retrieve", output_dict={"dosc": [1]})], state_keys=[]) == []
    assert _run([Ev("retrieve", output_dict={"docs": [1]})], state_keys=["docs"]) == []


def test_d1_extra_keys_that_are_not_typos_are_quiet():
    """A parsed reply's extra `reasoning` key, or `noise_a` beside a `noise_b`
    another node writes, is dropped by LangGraph but nothing was lost."""
    assert (
        _run(
            [Ev("classify", output_dict={"category": "x", "reasoning": "y"})],
            state_keys=["category"],
        )
        == []
    )
    ev = [Ev("A", output_dict={"noise_a": 1}), Ev("B", output_dict={"noise_b": 2})]
    assert _run(ev, state_keys=["noise_b", "answer"]) == []


def test_d1_subgraph_node_is_checked_against_its_own_schema():
    ev = [Ev("split", output_dict={"sections": [1]})]
    assert (
        _run(
            ev,
            state_keys=["text"],
            node_state_keys={"split": ["sections"]},
            consumers={"text": ["x"]},
        )
        == []
    )


# D2 ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "out",
    [
        [{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}],
        {"Fault": {"faultcode": "soap:Server", "faultstring": "Internal Error"}},
        {"__type": "ThrottlingException", "message": "Rate exceeded"},
        {"errorMessages": ["Issue does not exist"], "errors": {}},
        {"code": 20404, "message": "Not found", "status": 404},
        "<html><head><title>503 Service Unavailable</title></head></html>",
    ],
)
def test_d2_vendor_error_shapes(out):
    ev = [Ev("fetch", tool_calls=[{"name": "api", "input": "{}", "output": out, "error": None}])]
    assert _kinds(_run(ev)) == [("fetch", "error_response")]


def test_d2_a_successful_payload_is_quiet():
    ok = {"id": "00T5e09", "success": True, "errors": []}
    assert (
        _run([Ev("task", tool_calls=[{"name": "sf", "input": "{}", "output": ok, "error": None}])])
        == []
    )


# D3 ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "out",
    [
        {"totalSize": 0, "done": True, "records": []},
        {"Items": [], "Count": 0},
        {"kind": "bigquery#queryResponse", "totalRows": "0", "jobComplete": True},
        {"took": 3, "hits": {"total": {"value": 0}, "hits": []}},
        "No results found.",
    ],
)
def test_d3_an_empty_lookup_whatever_its_keys(out):
    ev = [
        Ev("lookup", tool_calls=[{"name": "search", "input": "{}", "output": out, "error": None}])
    ]
    assert _kinds(_run(ev)) == [("lookup", "empty_result")]


@pytest.mark.parametrize("out", [None, "", {"id": "x", "errors": []}, {"results": [{"id": 1}]}])
def test_d3_side_effect_tools_and_real_data_are_quiet(out):
    ev = [Ev("send", tool_calls=[{"name": "t", "input": "{}", "output": out, "error": None}])]
    assert _run(ev) == []


def test_d3_respects_allow_empty():
    ev = [
        Ev(
            "screen",
            output_dict={"hits": []},
            tool_calls=[
                {"name": "ofac", "input": "{}", "output": {"total": 0, "hits": []}, "error": None}
            ],
        )
    ]
    assert _run(ev, consumers={"hits": {"readers": ["decide"], "allow_empty": True}}) == []


# D4 ─────────────────────────────────────────────────────────────────────────


def test_d4_a_first_page_carried_forward_as_the_data():
    page = {"results": [{"id": 1}], "has_more": True, "next_page_token": "abc"}
    ev = [
        Ev(
            "fetch",
            output_dict={"rows": [{"id": 1}]},
            tool_calls=[{"name": "list", "input": "{}", "output": page, "error": None}],
        )
    ]
    assert _kinds(_run(ev)) == [("fetch", "unfollowed_pagination")]


def test_d4_taking_the_top_item_or_fetching_page_two_is_quiet():
    page = {"results": [{"id": 1}, {"id": 2}], "has_more": True}
    top = [
        Ev(
            "latest",
            output_dict={"order": {"id": 1}},
            tool_calls=[{"name": "list", "input": "{}", "output": page, "error": None}],
        )
    ]
    assert _run(top) == []
    both = [
        Ev(
            "fetch",
            output_dict={"rows": [1, 2, 3]},
            tool_calls=[
                {"name": "list", "input": "{}", "output": page, "error": None},
                {
                    "name": "list",
                    "input": "{'page': 2}",
                    "output": {"results": [{"id": 3}]},
                    "error": None,
                },
            ],
        )
    ]
    assert _run(both) == []


# D5 / D6 / D16 — healthy baseline ───────────────────────────────────────────


BASE = {
    "version": 1,
    "nodes": {
        "score": {
            "keys": ["scoring"],
            "types": {"scoring": "dict"},
            "paths": {"scoring.tier": "text", "scoring.score": "nonneg"},
        }
    },
}


def test_d5_type_drift_against_the_baseline():
    ev = [Ev("score", output_dict={"scoring": '{"tier": "hot"}'})]
    assert ("score", "type_drift") in _kinds(_run(ev, baseline=BASE))


@pytest.mark.parametrize("scoring", [{"tier": "N/A", "score": 80}, {"tier": "hot", "score": -1}])
def test_d6_sentinel_where_the_healthy_run_had_data(scoring):
    assert _kinds(_run([Ev("score", output_dict={"scoring": scoring})], baseline=BASE)) == [
        ("score", "sentinel_value")
    ]


def test_d16_a_key_the_node_always_writes_is_missing():
    assert _kinds(_run([Ev("score", output_dict={})], baseline=BASE)) == [
        ("score", "missing_output_key")
    ]


def test_d6_a_record_replaced_by_a_sentinel_string():
    base = {
        "version": 1,
        "nodes": {
            "retrieve": {
                "keys": ["docs"],
                "types": {"docs": "list"},
                "paths": {"docs[0]": "object", "docs[0].text": "text"},
            }
        },
    }
    assert _kinds(_run([Ev("retrieve", output_dict={"docs": ["N/A"]})], baseline=base)) == [
        ("retrieve", "sentinel_value")
    ]


def test_baseline_rules_are_quiet_on_a_matching_run_and_without_a_baseline():
    ok = [Ev("score", output_dict={"scoring": {"tier": "cold", "score": 12}})]
    assert _run(ok, baseline=BASE) == []
    assert _run([Ev("score", output_dict={})]) == []


def test_build_baseline_keeps_only_what_every_healthy_run_agrees_on():
    run1 = [Ev("score", output_dict={"scoring": {"tier": "hot", "score": 90}, "note": "x"})]
    run2 = [Ev("score", output_dict={"scoring": {"tier": "cold", "score": 10}})]
    base = R.build_baseline([run1, run2])
    node = base["nodes"]["score"]
    assert node["keys"] == ["scoring"]
    assert node["paths"] == {
        "scoring": "object",
        "scoring.tier": "text",
        "scoring.score": "nonneg",
    }
    assert "note" not in node["types"]


# D8 / D9 / D10 / D11 — model output ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Hi {{first_name}}, following up.",
        "Lorem ipsum dolor sit amet.",
        "Dear [Customer Name], thanks.",
        "<answer>",
    ],
)
def test_d8_unrendered_template_from_a_model_node(text):
    ev = [Ev("draft", output_dict={"reply": text}, llm_usage=_llm(text))]
    assert _kinds(_run(ev)) == [("draft", "unrendered_template")]


def test_d8_needs_a_model_call_and_skips_forwarded_text():
    assert _run([Ev("draft", output_dict={"reply": "Hi {{first_name}}"})]) == []
    fwd = [
        Ev(
            "comply",
            input_state={"reply": "Hi {{x}}"},
            output_dict={"approved": "Hi {{x}}"},
            llm_usage=_llm(),
        )
    ]
    assert _run(fwd) == []


def test_d9_degenerate_repetition():
    text = " ".join(["the order the"] * 12)
    assert _kinds(_run([Ev("answer", output_dict={"reply": text}, llm_usage=_llm(text))])) == [
        ("answer", "degenerate_repetition")
    ]


def test_d10_a_cut_off_generation_that_was_used():
    text = "Q3 revenue was $1,242,430 and September was the strongest mo"
    ev = [Ev("summarize", output_dict={"answer": text}, llm_usage=_llm(text, finish="length"))]
    assert _kinds(_run(ev)) == [("summarize", "truncated_output")]


def test_d10_a_one_word_classification_at_max_tokens_is_quiet():
    ev = [
        Ev(
            "classify",
            output_dict={"category": "billing"},
            llm_usage=_llm("billing", finish="length"),
        )
    ]
    assert _run(ev) == []


def test_d11_unparseable_model_json():
    raw = 'Sure! Here is the JSON:\n```json\n{"intent": "refund", "order_id": \n```'
    ev = [Ev("classify", output_dict={"triage": {"intent": "other"}}, llm_usage=_llm(raw))]
    assert _kinds(_run(ev)) == [("classify", "unparseable_model_json")]


def test_d11_valid_json_or_prose_is_quiet():
    good = '```json\n{"intent": "refund"}\n```'
    assert (
        _run([Ev("classify", output_dict={"triage": {"intent": "refund"}}, llm_usage=_llm(good))])
        == []
    )
    assert _run([Ev("answer", output_dict={"reply": "Thanks!"}, llm_usage=_llm("Thanks!"))]) == []


# D12 / D13 / D14 / D15 — grounding ──────────────────────────────────────────


def test_d12_a_number_no_input_contains():
    ev = [
        Ev(
            "answer",
            input_state={"order": {"total": 59.99}},
            output_dict={"reply": "I've refunded $49.99 for your order."},
            llm_usage=_llm(),
        )
    ]
    assert ("answer", "ungrounded_number") in _kinds(_run(ev))


def test_d12_sums_differences_and_word_multipliers_are_grounded():
    ev = [
        Ev(
            "summarize",
            input_state={"rows": [{"rev": 400.0}, {"rev": 452.3}], "funding": "$40M"},
            output_dict={"answer": "Revenue totalled $852.30, up 13.1%. They raised $40 million."},
            llm_usage=_llm(),
        )
    ]
    assert _run(ev) == []


def test_d13_a_near_miss_identifier_in_tool_args():
    ev = [
        Ev(
            "lookup",
            input_state={"ticket": "order A-1001 arrived broken"},
            tool_calls=[
                {
                    "name": "orders",
                    "input": "{'order_id': 'A-1002'}",
                    "output": {"orders": [{"id": "A-1002"}]},
                    "error": None,
                }
            ],
        )
    ]
    assert _kinds(_run(ev)) == [("lookup", "near_miss_identifier")]


def test_d13_constants_and_exact_ids_are_quiet():
    ev = [
        Ev(
            "send",
            input_state={"ticket": "order A-1001"},
            tool_calls=[
                {
                    "name": "mail",
                    "input": "{'to': 'support@example.com', 'ref': 'A-1001'}",
                    "output": {"ok": True},
                    "error": None,
                }
            ],
        )
    ]
    assert _run(ev) == []


def test_d14_an_action_claimed_that_no_tool_performed():
    ev = [
        Ev(
            "answer",
            output_dict={"reply": "I've refunded $59.99 to your card."},
            input_state={"total": 59.99},
            llm_usage=_llm(),
        )
    ]
    assert _kinds(_run(ev)) == [("answer", "unperformed_action")]


def test_d14_quiet_when_the_tool_ran_later_or_the_claim_is_negated():
    ok = [
        Ev(
            "answer",
            output_dict={"reply": "I've refunded $59.99."},
            input_state={"total": 59.99},
            llm_usage=_llm(),
        ),
        Ev(
            "refund",
            tool_calls=[
                {
                    "name": "stripe_refund",
                    "input": "{}",
                    "output": {"status": "succeeded"},
                    "error": None,
                }
            ],
        ),
    ]
    assert _run(ok) == []
    neg = [
        Ev("answer", output_dict={"reply": "No refunds were issued in March."}, llm_usage=_llm())
    ]
    assert _run(neg) == []


def test_d15_a_loop_that_repeats_the_same_call():
    ev = []
    for _ in range(3):
        ev.append(
            Ev(
                "agent",
                output_dict={"next_action": {"tool": "okta_user", "args": {"login": "a@b.com"}}},
            )
        )
        ev.append(
            Ev(
                "act",
                tool_calls=[
                    {
                        "name": "okta_user",
                        "input": "{'login': 'a@b.com'}",
                        "output": {"id": 1},
                        "error": None,
                    }
                ],
            )
        )
    hits = _run(ev)
    assert _kinds(hits) == [("agent", "stuck_loop")]
    assert hits[0].event is ev[-2], (
        "blame the decider's last visit, which survives the retry relabel"
    )


# consequence rule ───────────────────────────────────────────────────────────


def test_a_step_after_an_existing_origin_is_not_blamed_again():
    from argus.models import InspectionResult, ToolFailure

    bad = InspectionResult(
        is_silent_failure=True,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="x",
        tool_failures=[ToolFailure("error_response", "api.error", "critical", "x")],
    )
    ev = [
        Ev("fetch", inspection=bad),
        Ev(
            "lookup",
            tool_calls=[
                {
                    "name": "search",
                    "input": "{}",
                    "output": {"total": 0, "hits": []},
                    "error": None,
                }
            ],
        ),
    ]
    assert _run(ev) == []


# end to end, through the recorder ───────────────────────────────────────────

from typing import TypedDict  # noqa: E402

pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402

from argus.check import evaluate_run  # noqa: E402
from argus.recorder import ArgusRecorder  # noqa: E402
from argus.storage import load_run  # noqa: E402


class _S(TypedDict, total=False):
    ticket: str
    triage: dict
    reply: str


def _app(triage_update, reply="Thanks, we are on it."):
    g = StateGraph(_S)
    g.add_node("classify", lambda s: triage_update)
    g.add_node("answer", lambda s: {"reply": reply})
    g.add_edge(START, "classify")
    g.add_edge("classify", "answer")
    g.add_edge("answer", END)
    return g.compile()


def _grade(app, **kw):
    rec = ArgusRecorder(semantic_judge=False, **kw)
    rec.attach(app).invoke({"ticket": "refund please"})
    record = load_run(rec.session.run_id)
    return record, evaluate_run(record)


@pytest.mark.integration
def test_a_baseline_from_healthy_runs_fails_a_sentinel_regression(tmp_path):
    from argus.cli.cmd_baseline import baseline_for_runs

    healthy, v = _grade(_app({"triage": {"intent": "refund", "priority": 2}}))
    assert v.passed
    base = baseline_for_runs([healthy.run_id], tmp_path / "b.json")
    assert base["nodes"]["classify"]["paths"] == {
        "triage": "object",
        "triage.intent": "text",
        "triage.priority": "nonneg",
    }

    record, v = _grade(_app({"triage": {"intent": "unknown", "priority": 2}}), baseline=base)
    assert not v.passed and v.failing_nodes == ("classify",), v
    assert [f.type for f in record.findings if f.severity == "critical"] == ["sentinel_value"]

    _, v = _grade(_app({"triage": {"intent": "billing", "priority": 1}}), baseline=base)
    assert v.passed, v


@pytest.mark.integration
def test_a_typo_key_is_blamed_on_its_writer_not_on_the_first_step():
    """LangGraph silently drops `traige`. Before D1 the contextual layer's
    "never written" guess blamed the first step instead of the writer."""
    g = StateGraph(_S)
    g.add_node("intake", lambda s: {"ticket": s["ticket"].strip()})
    g.add_node("classify", lambda s: {"traige": {"intent": "refund"}})
    g.add_node(
        "answer",
        lambda s: {"reply": f"Routed as {(s.get('triage') or {}).get('intent', 'general')}."},
    )
    g.add_edge(START, "intake")
    g.add_edge("intake", "classify")
    g.add_edge("classify", "answer")
    g.add_edge("answer", END)
    record, v = _grade(g.compile(), consumers={"triage": ["answer"]})
    assert not v.passed
    assert v.failing_nodes == ("classify",), v
    assert "unknown_state_key" in {f.type for f in record.findings}


def test_the_model_call_keeps_its_output_text_clipped():
    from argus.llm_tracker import call_from_llm_outputs

    msg = {
        "lc": 1,
        "kwargs": {"content": "x" * 5000, "response_metadata": {"finish_reason": "stop"}},
    }
    call = call_from_llm_outputs(
        {"generations": [[{"message": msg}]], "llm_output": {"token_usage": {"total_tokens": 9}}}
    )
    assert call is not None and call.output_text == "x" * 4000
    parts = {
        "lc": 1,
        "kwargs": {
            "content": [{"type": "text", "text": "hi "}, {"type": "text", "text": "there"}]
        },
    }
    call = call_from_llm_outputs(
        {"generations": [[{"message": parts}]], "llm_output": {"token_usage": {"total_tokens": 3}}}
    )
    assert call is not None and call.output_text == "hi there"


def test_d14_the_object_of_the_claim_can_match_the_tool():
    """Blind probe: "I've approved a refund" after `create_refund` succeeded was
    flagged (the verb stem `appro` is in no tool name)."""
    ev = [
        Ev(
            "issue",
            tool_calls=[
                {
                    "name": "create_refund",
                    "input": "{}",
                    "output": {"status": "succeeded"},
                    "error": None,
                }
            ],
        ),
        Ev(
            "answer",
            input_state={"order_id": "A-1001"},
            output_dict={"reply": "Hi Dana, I've approved a refund for order A-1001."},
            llm_usage=_llm(),
        ),
    ]
    assert _run(ev) == []


def test_d14_a_generic_verb_is_checked_through_its_object():
    """Blind probe: "I've processed your refund" with no refund call was missed
    ("processed" was not a claim verb)."""
    ev = [
        Ev(
            "answer",
            output_dict={"reply": "Good news - I've processed your refund of $12.00."},
            input_state={"total": "$12.00"},
            llm_usage=_llm(),
        )
    ]
    assert _kinds(_run(ev)) == [("answer", "unperformed_action")]
    vague = [
        Ev(
            "answer",
            output_dict={"reply": "Thanks, I've processed it and will follow up."},
            llm_usage=_llm(),
        )
    ]
    assert _run(vague) == []


def test_d2_names_the_http_code_so_a_404_can_be_reviewed():
    ev = [
        Ev(
            "check",
            tool_calls=[
                {
                    "name": "get_report",
                    "input": "{}",
                    "output": {"status": 404, "message": "Not Found"},
                    "error": None,
                }
            ],
        )
    ]
    (hit,) = _run(ev)
    assert "(HTTP 404)" in hit.evidence


# ── final-check vocabulary (blind_eval/final_probe.py) ───────────────────────


def _tool(name, output, update):
    return Ev(
        "node",
        output_dict=update,
        tool_calls=[{"name": name, "input": "{}", "output": output, "error": None}],
    )


def test_d2_a_relay_that_refused_every_recipient():
    ev = [
        _tool(
            "send_email",
            {"accepted": [], "rejected": ["p@corp.io"], "response": "550 5.1.1"},
            {"sent": True},
        )
    ]
    assert _kinds(_run(ev)) == [("node", "error_response")]
    ok = [
        _tool(
            "send_email", {"accepted": ["p@corp.io"], "rejected": [], "id": "m1"}, {"sent": True}
        )
    ]
    assert _run(ok) == []


def test_d4_an_unfinished_query_is_strict_not_pagination():
    rows = [{"region": "EMEA", "q3": 472000}]
    ev = [
        _tool(
            "warehouse", {"rows": rows, "jobComplete": False, "pageToken": "BEHC2"}, {"rows": rows}
        )
    ]
    assert _kinds(_run(ev)) == [("node", "incomplete_result")]
    done = [
        _tool("warehouse", {"rows": rows, "jobComplete": True, "totalRows": "1"}, {"rows": rows})
    ]
    assert _run(done) == []


def test_d17_a_pending_status_recorded_as_done():
    out = {"id": "pay_9", "status": "requires_approval"}
    ev = [_tool("create_payment", out, {"payment": {"id": "pay_9", "status": "scheduled"}})]
    assert _kinds(_run(ev)) == [("node", "status_overstated")]
    honest = [
        _tool("create_payment", out, {"payment": {"id": "pay_9", "status": "requires_approval"}})
    ]
    assert _run(honest) == []


def test_404_and_an_already_exists_409_are_judgement_calls():
    from argus.review import is_heuristic

    exists = _tool("create_account", {"status": 409, "message": "User already exists"}, {})
    stage = _tool(
        "update_ats", {"status_code": 409, "message": "Application is not in a stage"}, {}
    )
    ev = "returned an error payload (HTTP 409) the node stored as data"
    assert is_heuristic(
        "error_response",
        f"create_account {ev}",
        exists,
        "create_account",
    )
    assert not is_heuristic("error_response", f"update_ats {ev}", stage, "update_ats")
    assert not is_heuristic("error_response", f"x {ev}")  # 409 with nothing to read: strict
    assert is_heuristic("error_response", "tool `get_report`: HTTP 404 error response")
    assert not is_heuristic("error_response", "tool `get_po`: HTTP 500 error response")
    assert is_heuristic("status_overstated")
