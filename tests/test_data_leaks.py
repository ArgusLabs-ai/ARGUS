"""Canary: no value a pipeline produced leaves the machine unless the user opts in.

Each test plants a canary string and fails if it reaches an outbound payload
(or, for ``redact_keys``, the saved run file; for the run card and the sanitized fix prompt,
the card and the prompt text).
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

import pytest
from conftest import make_event, make_run_record

from argus import cloud, llm_proxy, storage
from argus.cloud_card import build_card
from argus.findings import collect_findings
from argus.fix_prompt import build_fix_prompt_for_record
from argus.models import (
    InspectionResult,
    JudgeScore,
    SemanticCheckResult,
    ToolFailure,
    ValidatorResult,
)

CANARY = "CANARY-SSN-123-45-6789"
CANARY_EMAIL = "canary@leak.test"
CANARY_NODE = "canary_node"


@pytest.fixture
def logged_in(monkeypatch):
    creds = cloud.Credentials("tok", "ref", "uid", CANARY_EMAIL, 9e12)
    monkeypatch.setattr(cloud, "_get_valid_credentials", lambda: creds)
    monkeypatch.setattr(cloud, "is_logged_in", lambda: True)
    monkeypatch.delenv("ARGUS_CLOUD_SYNC", raising=False)
    monkeypatch.delenv("ARGUS_HOSTED_LLM", raising=False)
    sent: list = []
    monkeypatch.setattr(cloud, "_supabase_request", lambda *a, **k: sent.append((a, k)))
    return sent


def test_logged_in_save_run_uploads_nothing_without_opt_in(logged_in, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_runs_path", lambda: tmp_path)
    storage.save_run(make_run_record(run_id="r1"))
    assert logged_in == []

    monkeypatch.setenv("ARGUS_CLOUD_SYNC", "1")
    storage.save_run(make_run_record(run_id="r2"))
    assert len(logged_in) == 1  # opted in: the full run goes up, as documented


def test_shared_signature_carries_no_evidence_nodes_or_email(logged_in):
    sig = {
        "id": "sig1",
        "category": "placeholder_outputs",
        "pattern": "lorem ipsum",
        "match_strategy": "contains_ci",
        "severity": "warning",
        "description": "placeholder text",
        "reasoning": f"saw {CANARY} in the output",
        "evidence": [CANARY],
        "source_nodes": [CANARY_NODE],
        "metadata": {"confidence": 0.9, "frequency": 3},
    }
    assert cloud.push_shared_signature(sig)
    payload = json.dumps(logged_in[0][1]["body"])
    for canary in (CANARY, CANARY_EMAIL, CANARY_NODE):
        assert canary not in payload


def test_hosted_llm_proxy_is_opt_in(logged_in, monkeypatch):
    calls: list = []

    def _urlopen(req, **k):
        calls.append(req.data)
        raise OSError("offline")

    monkeypatch.setattr(llm_proxy, "_PROXY_URL", "https://proxy.invalid")
    monkeypatch.setattr(llm_proxy, "SUPABASE_URL", "https://sb.invalid")
    monkeypatch.setattr(llm_proxy, "_get_valid_credentials", cloud._get_valid_credentials)
    monkeypatch.setattr(llm_proxy.urllib.request, "urlopen", _urlopen)

    msg = [{"role": "user", "content": CANARY}]
    assert "error" in llm_proxy._call_proxy(model="gpt-4.1", messages=msg)
    assert calls == []

    monkeypatch.setenv("ARGUS_HOSTED_LLM", "1")
    llm_proxy._call_proxy(model="gpt-4.1", messages=msg)
    assert len(calls) == 1


def test_recorder_redact_keys_keeps_value_out_of_run_file():
    pytest.importorskip("langchain_core")
    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder

    class S(TypedDict, total=False):
        customer: dict
        answer: str

    def lookup(state: S) -> dict:
        return {"customer": {"ssn": CANARY, "name": "x"}}

    def reply(state: S) -> dict:
        return {"answer": "done"}

    g = StateGraph(S)
    g.add_node("lookup", lookup)
    g.add_node("reply", reply)
    g.add_edge(START, "lookup")
    g.add_edge("lookup", "reply")
    g.add_edge("reply", END)

    recorder = ArgusRecorder(semantic_judge=False, redact_keys={"ssn"})
    recorder.attach(g.compile()).invoke({"customer": {"ssn": CANARY}})
    text = storage.load_run_text(recorder.session.run_id)
    assert CANARY not in text
    assert '"name": "x"' in text  # only the named key is scrubbed


# ── the run card and the sanitized fix prompt: a canary in every place a value can live ──
#
# One failed run (``lookup`` -> ``reply``). Each entry in PLANTS puts the canary in a single
# place, so a leak names the place; KITCHEN_SINK puts it in all of them at once.

_PLAIN_UPDATE: dict[str, Any] = {"balance": -1}
_PLAIN_EVIDENCE = "`balance` = -1; healthy runs hold real data here"

PLANTS: dict[str, dict[str, Any]] = {
    "initial_state": {"initial_state": {"customer": CANARY}},
    "input_state": {"input_state": {"customer": {CANARY: CANARY}}},
    "update_value": {"update": {"note": CANARY}},
    "update_nested_value": {"update": {"customer": {"ssn": CANARY}}},
    "update_nested_key": {"update": {"customer": {CANARY: 1}}},
    "update_nested_key_in_list": {"update": {"rows": [{CANARY: 1}]}},
    "update_top_level_key": {"update": {CANARY: 1}},
    "tool_output": {
        "tool_calls": [
            {"name": "crm", "input": "x", "output": {"status": 200, CANARY: CANARY}, "error": None}
        ]
    },
    "tool_error": {
        "tool_calls": [
            {"name": "crm", "input": "x", "output": None, "error": f"RuntimeError('{CANARY}')"}
        ],
        "tool_failure": ToolFailure(
            "tool_error", "crm", "critical", f"tool `crm` raised RuntimeError('{CANARY}')"
        ),
    },
    "sentinel_reason": {"evidence": f"`balance` = {CANARY!r}; healthy runs hold real data here"},
    "finding_field_path": {"field": f"customer.{CANARY}.balance"},
    "judge_explanation": {"judge_explanation": CANARY},
    "validator_message": {"validator_message": CANARY},
    "exception_message": {"exception": f"KeyError: '{CANARY}'"},
}
KITCHEN_SINK: dict[str, Any] = {
    "initial_state": {"customer": CANARY, CANARY: CANARY},
    "input_state": {"customer": {CANARY: CANARY}, CANARY: CANARY},
    "update": {
        "note": CANARY,
        "customer": {"ssn": CANARY, CANARY: 1},
        "rows": [{CANARY: 1}],
        CANARY: 1,
    },
    "tool_calls": PLANTS["tool_output"]["tool_calls"] + PLANTS["tool_error"]["tool_calls"],
    "tool_failure": PLANTS["tool_error"]["tool_failure"],
    "evidence": PLANTS["sentinel_reason"]["evidence"],
    "field": PLANTS["finding_field_path"]["field"],
    "judge_explanation": CANARY,
    "validator_message": CANARY,
    "exception": f"KeyError: '{CANARY}'",
}
# Where the unsanitized prompt prints the value, so the sanitized assertion is not vacuous.
PRINTED_BY_THE_PLAIN_PROMPT = [
    "input_state",
    "update_value",
    "update_nested_value",
    "update_nested_key",
    "update_top_level_key",
    "tool_error",
    "sentinel_reason",
    "finding_field_path",
    "exception_message",
]


def _canary_record(**planted: Any):
    """A failed run whose every value is benign except where ``planted`` puts the canary."""
    update = planted.get("update", _PLAIN_UPDATE)
    field = planted.get("field", "balance")
    tool_failure = planted.get("tool_failure") or ToolFailure(
        "sentinel_value", field, "critical", planted.get("evidence", _PLAIN_EVIDENCE)
    )
    judge_explanation = planted.get("judge_explanation", "looks wrong")
    lookup = make_event(
        node_name="lookup",
        status="fail",
        input_state=planted.get("input_state", {"customer": "ana"}),
        output=update,
        exception=planted.get("exception"),
        inspection=InspectionResult(
            is_silent_failure=True,
            missing_fields=[],
            empty_fields=[],
            type_mismatches=[],
            severity="critical",
            message="sentinel",
            tool_failures=[tool_failure],
            has_tool_failure=True,
        ),
        validator_results=[
            ValidatorResult("*:check", False, planted.get("validator_message", "bad"))
        ],
        semantic_check=SemanticCheckResult(
            passed=False,
            reason=judge_explanation,
            confidence=0.9,
            model="gpt-4o-mini",
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1.0,
        ),
    )
    lookup.tool_calls = planted.get("tool_calls", [])
    reply = make_event(node_name="reply", step_index=1, output={"answer": "ok"})
    record = make_run_record(
        events=[lookup, reply],
        edges={"lookup": ["reply"], "reply": []},
        status="silent_failure",
        run_id="20261017-101010-abc123",
    )
    record.first_failure_step = "lookup"
    record.root_cause_chain = ["lookup"]
    record.initial_state = planted.get("initial_state", {"customer": "ana"})
    record.judge_scores = [
        JudgeScore(
            0.2,
            label="low",
            explanation=judge_explanation,
            validator_name="*:judge",
            severity="warning",
            origin_node="lookup",
            step_index=0,
        )
    ]
    record.findings = collect_findings(record.steps, judge_scores=record.judge_scores)
    return record


def _leaks(text: str) -> list[str]:
    return [piece for piece in (CANARY, "123-45-6789") if piece.lower() in text.lower()]


@pytest.mark.parametrize("place", sorted(PLANTS))
def test_sanitized_fix_prompt_carries_no_canary(place):
    prompt = build_fix_prompt_for_record(_canary_record(**PLANTS[place]), sanitized=True).prompt
    assert _leaks(prompt) == []


def test_sanitized_fix_prompt_carries_no_canary_from_all_places_at_once():
    prompt = build_fix_prompt_for_record(_canary_record(**KITCHEN_SINK), sanitized=True).prompt
    assert _leaks(prompt) == []


@pytest.mark.parametrize("place", PRINTED_BY_THE_PLAIN_PROMPT)
def test_the_unsanitized_fix_prompt_still_prints_what_sanitizing_removes(place):
    prompt = build_fix_prompt_for_record(_canary_record(**PLANTS[place])).prompt
    assert _leaks(prompt), f"the plain prompt no longer shows the canary planted in {place}"


@pytest.mark.parametrize("place", sorted(PLANTS))
def test_run_card_carries_no_canary(place):
    card = build_card(_canary_record(**PLANTS[place]))
    assert _leaks(json.dumps(card)) == []


def test_run_card_carries_no_canary_from_all_places_at_once():
    card = build_card(_canary_record(**KITCHEN_SINK))
    assert _leaks(json.dumps(card)) == []
    # ...and it is not vacuous: the pipeline's own names and the verdict are still there
    assert card["run"]["overall_status"] == "silent_failure"
    assert [s["node"] for s in card["steps"]] == ["lookup", "reply"]
    assert card["findings"] and card["fix_prompt"]


def test_run_card_findings_have_no_reason_even_when_the_reason_quotes_the_value():
    record = _canary_record(**PLANTS["sentinel_reason"])
    assert any(CANARY in f.reason for f in record.findings)  # the local finding does quote it
    assert all("reason" not in f for f in build_card(record)["findings"])


def test_a_findings_id_is_not_a_hash_of_the_raw_path():
    record = _canary_record(**PLANTS["finding_field_path"])
    raw_ids = {f.id for f in record.findings if CANARY in (f.field_path or "")}
    assert raw_ids  # the local ids do hash a path that holds the canary
    sent = json.dumps(build_card(record))
    assert not any(raw_id in sent for raw_id in raw_ids)


def test_run_card_from_a_real_pipeline_leaks_nothing():
    pytest.importorskip("langchain_core")
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder

    class S(TypedDict, total=False):
        customer: dict
        note: str

    @tool
    def crm(q: str) -> dict:
        """Look up a customer."""
        return {"status_code": 200, CANARY: CANARY}

    @tool
    def mailer(q: str) -> dict:
        """Send mail."""
        raise RuntimeError(f"could not reach {CANARY}")

    def lookup(state: S) -> dict:
        crm.invoke({"q": state["note"]})
        try:
            mailer.invoke({"q": CANARY})
        except RuntimeError:
            pass  # swallowed: the tool error is recorded, the node carries on
        return {"customer": {CANARY: "N/A"}, "note": CANARY}  # D6: healthy runs held data here

    def reply(state: S) -> dict:
        return {"note": "done"}

    graph = StateGraph(S)
    graph.add_node("lookup", lookup)
    graph.add_node("reply", reply)
    graph.add_edge(START, "lookup")
    graph.add_edge("lookup", "reply")
    graph.add_edge("reply", END)
    baseline = {
        "version": 1,
        "nodes": {
            "lookup": {
                "keys": ["customer"],
                "types": {"customer": "dict"},
                "paths": {"customer": "object", f"customer.{CANARY}": "text"},
            }
        },
    }

    recorder = ArgusRecorder(semantic_judge=False, baseline=baseline)
    recorder.attach(graph.compile()).invoke({"customer": {CANARY: "Ana"}, "note": CANARY})
    record = storage.load_run(recorder.run_ids[-1])
    card = build_card(record)

    sentinel = [f for f in card["findings"] if f["type"] == "sentinel_value"]
    assert sentinel and sentinel[0]["field_path"] == "customer.?"
    assert any(CANARY in f.reason for f in record.findings)  # the local record quotes the path
    assert {t["name"] for t in card["steps"][0]["tools"]} == {"crm", "mailer"}
    assert card["fix_prompt"]
    assert _leaks(json.dumps(card)) == []
