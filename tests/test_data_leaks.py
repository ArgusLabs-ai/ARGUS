"""Canary: no value a pipeline produced leaves the machine unless the user opts in.

Each test plants a canary string and fails if it reaches an outbound payload
(or, for ``redact_keys``, the saved run file; for the run card and the sanitized fix prompt,
the card and the prompt text).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

import pytest
from conftest import make_event, make_run_record
from fake_workspace import FakeWorkspace, point_at, serve
from typer.testing import CliRunner

from argus import cloud, llm_proxy, run_context, storage
from argus.cli.main import app
from argus.cloud_card import build_card
from argus.findings import collect_findings
from argus.fix_prompt import build_fix_prompt_for_record
from argus.models import (
    InspectionResult,
    JudgeScore,
    SemanticCheckResult,
    SemanticSignal,
    ToolFailure,
    ValidatorResult,
)

CANARY = "CANARY-SSN-123-45-6789"
CANARY_EMAIL = "canary@leak.test"
CANARY_NODE = "canary_node"
# A key with a dot in it. Each half looks like an identifier alone; only the whole is a value.
DOTTED = ("canary_first", "canary_second")
DOTTED_KEY = ".".join(DOTTED)
# What an LLM wrote about a pattern it saw in real outputs (learned and shared signatures).
CANARY_DESCRIPTION = "replies that quote the acme-refund-ledger"


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


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param({"ARGUS_REPORT_ONLY": "1"}, id="report-only env"),
        pytest.param({"ARGUS_REPORT_ONLY": "true"}, id="report-only env, spelled out"),
        pytest.param({"ARGUS_CLOUD_URL": "https://workspace.example.com"}, id="workspace url"),
        pytest.param({"plugin": "report-only"}, id="pytest --argus-report-only"),
    ],
)
def test_full_run_sync_never_runs_while_reporting_to_a_workspace(
    logged_in, tmp_path, monkeypatch, mode
):
    """A workspace gets run cards (argus cloud upload), never the run: not even from a user who
    is logged in and set ARGUS_CLOUD_SYNC=1 for their own machine."""
    from argus import run_context

    monkeypatch.setattr(storage, "_runs_path", lambda: tmp_path)
    monkeypatch.setenv("ARGUS_CLOUD_SYNC", "1")
    storage.save_run(make_run_record(run_id="control"))
    assert len(logged_in) == 1  # not vacuous: with the opt-in and no workspace, the run goes up

    for name, value in mode.items():
        if name == "plugin":
            run_context.set_report_only(True)
        else:
            monkeypatch.setenv(name, value)
    try:
        storage.save_run(make_run_record(run_id="in-workspace-mode"))
    finally:
        run_context.set_report_only(False)
    assert len(logged_in) == 1


@pytest.mark.parametrize("value", ["", "0", "false", " "])
def test_a_switch_that_is_off_leaves_the_opted_in_sync_alone(
    logged_in, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(storage, "_runs_path", lambda: tmp_path)
    monkeypatch.setenv("ARGUS_CLOUD_SYNC", "1")
    monkeypatch.setenv("ARGUS_REPORT_ONLY", value)
    monkeypatch.setenv("ARGUS_CLOUD_URL", " ")  # a blank URL is no URL
    storage.save_run(make_run_record(run_id="r1"))
    assert len(logged_in) == 1


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
    # A dict key that contains a dot, and a finding whose path runs through it.
    "dotted_key_in_update": {
        "update": {"accounts": {DOTTED_KEY: {"balance": -1}}},
        "field": f"accounts.{DOTTED_KEY}.balance",
    },
    "dotted_top_level_key_in_update": {
        "update": {DOTTED_KEY: {"balance": -1}},
        "field": f"{DOTTED_KEY}.balance",
    },
    "dotted_key_in_input_state": {
        "input_state": {"accounts": {DOTTED_KEY: 1}},
        "field": f"accounts.{DOTTED_KEY}",
    },
    "dotted_key_in_tool_output": {
        "tool_calls": [
            {
                "name": "crm",
                "input": "x",
                "output": {"status": 200, "accounts": {DOTTED_KEY: {"error": "x"}}},
                "error": None,
            }
        ],
        "field": f"crm.accounts.{DOTTED_KEY}.error",
    },
    "dotted_key_in_a_list_item": {
        "update": {"rows": [{DOTTED_KEY: 1}]},
        "field": f"rows[0].{DOTTED_KEY}",
    },
    # A signature the project or the community added: its description is text an LLM wrote.
    "learned_signature_description": {
        "signal": SemanticSignal(
            "CS-001", "learned_pattern", "critical", CANARY_DESCRIPTION, ("note",), "ev"
        )
    },
    "shared_signature_description": {
        "signal": SemanticSignal(
            "CS-014", "shared_pattern", "warning", CANARY_DESCRIPTION, ("note",), "ev"
        )
    },
    "bundled_id_with_other_text": {  # a learned signature cannot borrow a bundled id
        "signal": SemanticSignal(
            "PH-001", "placeholder_outputs", "warning", CANARY_DESCRIPTION, ("note",), "ev"
        )
    },
    "signature_no_longer_known": {
        "signal": SemanticSignal("ZZ-999", "gone", "warning", CANARY_DESCRIPTION, ("note",), "ev")
    },
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
    "signal": PLANTS["learned_signature_description"]["signal"],
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
    "dotted_key_in_update",
    "dotted_top_level_key_in_update",
    "dotted_key_in_input_state",
    "dotted_key_in_tool_output",
    "dotted_key_in_a_list_item",
    "learned_signature_description",
    "shared_signature_description",
    "bundled_id_with_other_text",
    "signature_no_longer_known",
]
BUNDLED_SIGNATURE = json.loads(
    (Path(__file__).parent.parent / "src/argus/data/signatures.json").read_text(encoding="utf-8")
)["signatures"][0]


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
            semantic_signals=[planted["signal"]] if "signal" in planted else [],
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
    pieces = (CANARY, "123-45-6789", *DOTTED, CANARY_DESCRIPTION)
    return [piece for piece in pieces if piece.lower() in text.lower()]


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


# ── a key that contains a dot, and the description of a signature the project added ──


def test_a_dotted_key_is_masked_whole_in_the_card_and_the_sanitized_prompt():
    record = _canary_record(**PLANTS["dotted_key_in_update"])
    (finding,) = [f for f in build_card(record)["findings"] if f["type"] == "sentinel_value"]
    assert finding["field_path"] == "accounts.?.?.?"
    prompt = build_fix_prompt_for_record(record, sanitized=True).prompt
    assert "accounts.?.?.?" in prompt
    assert _leaks(prompt) == [] and _leaks(json.dumps(build_card(record))) == []


def test_a_dotted_key_the_data_does_not_hold_is_masked_too():
    # The path names a key the update no longer has (a baseline's "lost" key): nothing to walk.
    record = _canary_record(field=DOTTED_KEY)
    (finding,) = [f for f in build_card(record)["findings"] if f["type"] == "sentinel_value"]
    assert finding["field_path"] == "?.?"
    assert _leaks(build_fix_prompt_for_record(record, sanitized=True).prompt) == []


def test_a_names_that_are_real_keys_are_still_shown():
    # Not vacuous: the rule masks dotted keys, not every nested name.
    record = _canary_record(
        update={"accounts": {"ana": {"balance": -1}}}, field="accounts.ana.balance"
    )
    (finding,) = [f for f in build_card(record)["findings"] if f["type"] == "sentinel_value"]
    assert finding["field_path"] == "accounts.ana.balance"
    assert "accounts.ana.balance" in build_fix_prompt_for_record(record, sanitized=True).prompt


def test_a_bundled_signatures_description_stays_in_the_sanitized_prompt():
    text = BUNDLED_SIGNATURE["description"]
    signal = SemanticSignal(
        BUNDLED_SIGNATURE["id"], BUNDLED_SIGNATURE["category"], "warning", text, ("note",), "ev"
    )
    prompt = build_fix_prompt_for_record(_canary_record(signal=signal), sanitized=True).prompt
    assert text in prompt


def test_a_signature_the_project_added_is_named_in_the_prompt_without_its_description():
    signal = PLANTS["learned_signature_description"]["signal"]
    prompt = build_fix_prompt_for_record(_canary_record(signal=signal), sanitized=True).prompt
    assert CANARY_DESCRIPTION not in prompt
    assert prompt.count("a signature added to this project") == 2  # what went wrong, done when


def test_run_card_from_a_real_pipeline_with_a_dotted_key_leaks_nothing():
    pytest.importorskip("langchain_core")
    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder

    class S(TypedDict, total=False):
        accounts: dict
        note: str

    def lookup(state: S) -> dict:
        return {"accounts": {DOTTED_KEY: "N/A"}}  # D6: healthy runs held data here

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
                "keys": ["accounts"],
                "types": {"accounts": "dict"},
                "paths": {"accounts": "object", f"accounts.{DOTTED_KEY}": "text"},
            }
        },
    }

    recorder = ArgusRecorder(semantic_judge=False, baseline=baseline)
    recorder.attach(graph.compile()).invoke({"accounts": {DOTTED_KEY: "Ana"}})
    record = storage.load_run(recorder.run_ids[-1])
    card = build_card(record)

    sentinel = [f for f in card["findings"] if f["type"] == "sentinel_value"]
    assert sentinel and sentinel[0]["field_path"] == "accounts.?.?"
    assert any(DOTTED_KEY in f.reason for f in record.findings)  # the local record quotes the path
    assert card["fix_prompt"] and "accounts.?.?" in card["fix_prompt"]
    assert _leaks(json.dumps(card)) == []


def test_run_card_from_a_real_pipeline_with_a_learned_signature_leaks_nothing():
    pytest.importorskip("langchain_core")
    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder
    from argus.registry import reload_registry

    custom = {
        "signatures": [
            {
                "id": "CS-001",
                "category": "learned_pattern",
                "pattern": "needle-phrase",
                "match_strategy": "contains_ci",
                "severity": "critical",
                "description": CANARY_DESCRIPTION,
                "source": "learned",
                "metadata": {"approval_status": "approved"},
            }
        ]
    }
    (Path(".argus") / "custom_signatures.json").write_text(json.dumps(custom), encoding="utf-8")
    reload_registry()

    class S(TypedDict, total=False):
        note: str

    def lookup(state: S) -> dict:
        return {"note": "the needle-phrase is in this reply"}

    def reply(state: S) -> dict:
        return {"note": "done"}

    graph = StateGraph(S)
    graph.add_node("lookup", lookup)
    graph.add_node("reply", reply)
    graph.add_edge(START, "lookup")
    graph.add_edge("lookup", "reply")
    graph.add_edge("reply", END)

    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(graph.compile()).invoke({"note": "hi"})
    record = storage.load_run(recorder.run_ids[-1])
    signals = [s for e in record.steps if e.inspection for s in e.inspection.semantic_signals]
    assert [s.sig_id for s in signals] == ["CS-001"]  # the learned signature fired
    assert (
        CANARY_DESCRIPTION in build_fix_prompt_for_record(record).prompt
    )  # and the plain prompt says it
    card = build_card(record)
    assert card["fix_prompt"]
    assert _leaks(json.dumps(card)) == []


# ── the upload: the body is the card and nothing else ────────────────────────


@pytest.fixture
def workspace(monkeypatch):
    fake = FakeWorkspace()
    with serve(fake):
        point_at(monkeypatch, fake)
        yield fake


def _uploaded_cards(workspace, *records):
    """Save ``records`` as one pytest session does, run ``argus cloud upload`` and return the
    raw request body and the cards in it."""
    run_context.begin_session(storage.session_path(), fresh=True)
    try:
        for record in records:
            storage.save_run(record)
    finally:
        run_context.end_session()
    result = CliRunner().invoke(app, ["cloud", "upload", "--final"])
    assert result.exit_code == 0, result.output
    (post,) = workspace.posts()
    return post["body"].decode("utf-8"), json.loads(post["body"])["cards"]


@pytest.mark.parametrize("place", sorted(PLANTS))
def test_the_uploaded_body_is_the_card_and_carries_no_canary(workspace, place):
    record = _canary_record(**PLANTS[place])
    text, cards = _uploaded_cards(workspace, record)
    assert cards == [build_card(storage.load_run(record.run_id))]
    assert _leaks(text) == []


def test_the_uploaded_body_carries_no_canary_from_all_places_at_once(workspace):
    record = _canary_record(**KITCHEN_SINK)
    text, cards = _uploaded_cards(workspace, record)
    assert cards == [build_card(storage.load_run(record.run_id))]
    assert _leaks(text) == []
    # not vacuous: the verdict and the pipeline's own names did go up
    (card,) = cards
    assert card["run"]["overall_status"] == "silent_failure"
    assert [s["node"] for s in card["steps"]] == ["lookup", "reply"] and card["fix_prompt"]


def test_what_the_runner_saved_to_disk_is_not_what_the_upload_carries(workspace):
    # The run file holds every value (that is what `argus ui` shows). The upload reads the file
    # and still sends the card, so a canary in the file is the proof the card is built, not copied.
    record = _canary_record(**KITCHEN_SINK)
    assert CANARY in json.dumps(storage._to_json_serializable(record))
    text, _ = _uploaded_cards(workspace, record)
    assert CANARY not in text and CANARY_EMAIL not in text


def test_a_dotted_key_in_one_iteration_is_not_shown_because_another_iteration_has_nested_keys():
    """A node that ran twice: one update has the dotted key, the other has the same words as
    nested keys. The path must not be read against the second to show the first's key."""
    first, second = DOTTED
    path = f"accounts.{DOTTED_KEY}.balance"
    dotted = make_event(node_name="lookup", output={"accounts": {DOTTED_KEY: {"balance": -1}}})
    nested = make_event(
        node_name="lookup", step_index=1, output={"accounts": {first: {second: {"balance": 2}}}}
    )
    reply = make_event(node_name="reply", step_index=2, output={"answer": "ok"})
    record = _canary_record(field=path)
    record.steps = [dotted, nested, reply]
    (finding,) = [f for f in build_card(record)["findings"] if f["type"] == "sentinel_value"]
    assert finding["field_path"] == "accounts.?.?.?"
    assert _leaks(json.dumps(build_card(record))) == []
