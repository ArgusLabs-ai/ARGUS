"""Run card (``argus.cloud_card``): the shape-only payload a team workspace may receive.

The card is built by allowlist from named fields: the verdict, the findings without their
``reason``, and the *shape* of the data, never a value. The leak canaries live in
tests/test_data_leaks.py and the naming rule in tests/test_naming_rule.py; these tests pin what
the card does carry and that every card validates against the published schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

import pytest
from conftest import make_event, make_run_record
from schema_check import validate

from argus import cloud_card
from argus.cloud_card import build_card
from argus.findings import _fid, collect_findings
from argus.fix_prompt import build_fix_prompt_for_record
from argus.models import (
    Finding,
    InspectionResult,
    LLMCallInfo,
    LLMUsage,
    ToolFailure,
)

CANARY = "CANARY-SSN-123-45-6789"
ROOT = Path(__file__).parent.parent
SCHEMA = json.loads((ROOT / "src/argus/data/cloud_card.schema.json").read_text(encoding="utf-8"))


# ── records to build cards from ──────────────────────────────────────────────


def _inspection(*failures: ToolFailure) -> InspectionResult:
    return InspectionResult(
        is_silent_failure=True,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="critical",
        message="failed",
        tool_failures=list(failures),
        has_tool_failure=True,
    )


def _clean_record():
    fetch = make_event(node_name="fetch", output={"docs": ["a", "b"]}, duration_ms=12.5)
    fetch.llm_usage = LLMUsage(
        calls=[LLMCallInfo("gpt-4o", 10, 5, 15, cost_usd=0.01, finish_reason="stop")]
    )
    reply = make_event(node_name="reply", step_index=1, output={"answer": "Hello there"})
    record = make_run_record(
        events=[fetch, reply], edges={"fetch": ["reply"], "reply": []}, run_id="20261017-101010-a1"
    )
    record.agent = "support-bot"
    record.environment = "ci"
    record.end_reason = "finished"
    return record


def _failing_record():
    fetch = make_event(
        node_name="fetch",
        status="fail",
        output={"docs": [], "ok": False, "customer": {"email": "ana@acme.com"}},
        inspection=_inspection(
            ToolFailure("empty_result", "docs", "critical", "empty `docs`"),
            ToolFailure("error_response", "crm.status", "critical", "HTTP 503 from crm"),
        ),
    )
    fetch.tool_calls = [
        {"name": "crm", "input": "q", "output": {"status_code": 503}, "error": None}
    ]
    reply = make_event(node_name="reply", step_index=1, output={"answer": "sorry"})
    record = make_run_record(
        events=[fetch, reply],
        edges={"fetch": ["reply"], "reply": []},
        status="silent_failure",
        run_id="20261017-101010-b2",
    )
    record.first_failure_step = "fetch"
    record.root_cause_chain = ["fetch"]
    record.findings = collect_findings(record.steps)
    return record


def _crashed_record():
    fetch = make_event(node_name="fetch", output={"docs": ["a"]})
    reply = make_event(
        node_name="reply",
        status="crashed",
        step_index=1,
        output=None,
        exception="Traceback (most recent call last):\nKeyError: 'summary'",
    )
    record = make_run_record(
        events=[fetch, reply],
        edges={"fetch": ["reply"], "reply": []},
        status="crashed",
        run_id="20261017-101010-c3",
    )
    record.first_failure_step = "reply"
    record.root_cause_chain = ["fetch", "reply"]
    record.complete = False
    record.end_reason = "exception"
    record.findings = collect_findings(record.steps)
    return record


RECORDS = {"clean": _clean_record, "failing": _failing_record, "crashed": _crashed_record}


# ── the schema ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(RECORDS))
def test_every_kind_of_run_validates_against_the_published_schema(kind):
    validate(build_card(RECORDS[kind]()), SCHEMA)


@pytest.mark.parametrize("path", [["extra"], ["run", "extra"], ["steps", 0, "reason"]])
def test_the_schema_refuses_a_field_it_does_not_list(path):
    card = build_card(_failing_record())
    target: Any = card
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = "anything"
    with pytest.raises(AssertionError, match="unknown field"):
        validate(card, SCHEMA)


def test_the_schema_refuses_a_nested_key_name_in_a_shape():
    card = build_card(_failing_record())
    card["steps"][0]["update"]["keys"]["customer"]["keys"] = {"email": {"t": "string"}}
    with pytest.raises(AssertionError, match="unknown field"):
        validate(card, SCHEMA)


@pytest.mark.parametrize("key", [CANARY, "user-id", "1st", "a.b", "x" * 65])
def test_the_schema_refuses_an_update_key_that_is_not_an_identifier(key):
    card = build_card(_failing_record())
    card["steps"][0]["update"]["keys"][key] = {"t": "int"}
    with pytest.raises(AssertionError, match="does not match"):
        validate(card, SCHEMA)


@pytest.mark.parametrize(
    "path", [f"customer.{CANARY}", "a b", "a..b", "", "a.b.c.d.e.f.g.h.i", "a[x]", "a[0]b"]
)
def test_the_schema_refuses_a_field_path_that_names_a_value(path):
    card = build_card(_failing_record())
    card["findings"][0]["field_path"] = path
    with pytest.raises(AssertionError):
        validate(card, SCHEMA)


def test_the_sample_card_on_the_data_page_is_valid():
    page = (ROOT / "docs/DATA.md").read_text(encoding="utf-8")
    sample = page.split("## What a team workspace receives", 1)[1].split("```json", 1)[1]
    validate(json.loads(sample.split("```", 1)[0]), SCHEMA)


def test_the_schema_has_no_place_for_a_reason_a_value_or_a_nested_key():
    defs = SCHEMA["$defs"]
    assert "reason" not in defs["finding"]["properties"]
    assert set(defs["shape"]["properties"]) == {"t", "len", "f", "item"}  # no v, no keys


def test_the_validator_refuses_a_keyword_it_does_not_check():
    with pytest.raises(AssertionError, match="nothing here checks"):
        validate("x", {"type": "string", "format": "email"})


def test_the_validator_checks_patterns_against_the_whole_string():
    schema = {"type": "string", "pattern": "^[a-z]+$"}
    validate("abc", schema)
    for bad in ("abc\n", "ABC", ""):
        with pytest.raises(AssertionError):
            validate(bad, schema)


def test_the_schema_limits_are_the_ones_the_builder_applies():
    props = SCHEMA["properties"]
    assert props["steps"]["maxItems"] == cloud_card.MAX_STEPS
    assert props["findings"]["maxItems"] == cloud_card.MAX_FINDINGS
    assert props["llm_usage"]["maxItems"] == cloud_card.MAX_MODELS
    assert props["root_cause_chain"]["maxItems"] == cloud_card.MAX_CHAIN
    assert props["topology"]["properties"]["graph_node_names"]["maxItems"] == cloud_card.MAX_NODES
    edges = props["topology"]["properties"]["graph_edge_map"]
    assert edges["maxProperties"] == cloud_card.MAX_NODES
    assert edges["additionalProperties"]["maxItems"] == cloud_card.MAX_EDGES
    assert SCHEMA["$defs"]["step"]["properties"]["tools"]["maxItems"] == cloud_card.MAX_TOOLS
    assert SCHEMA["$defs"]["update"]["properties"]["keys"]["maxProperties"] == cloud_card.MAX_KEYS
    assert props["fix_prompt"]["maxLength"] == cloud_card.MAX_PROMPT
    assert SCHEMA["$defs"]["name"]["maxLength"] == cloud_card.MAX_NAME


# ── what the card carries ────────────────────────────────────────────────────


def test_run_section_carries_the_verdict_and_identity():
    record = _crashed_record()
    record.agent, record.environment = "support-bot", "ci"
    run = build_card(record)["run"]
    assert run == {
        "run_id": "20261017-101010-c3",
        "argus_version": "0.8.11",
        "overall_status": "crashed",
        "complete": False,
        "end_reason": "exception",
        "duration_ms": 1000.0,
        "started_at": record.started_at,
        "agent": "support-bot",
        "environment": "ci",
        "test_id": None,
    }


def test_run_section_for_a_run_that_has_no_identity_yet():
    run = build_card(make_run_record())["run"]
    assert run["agent"] is None and run["environment"] is None and run["end_reason"] is None


def test_topology_carries_the_node_names_and_the_edge_map():
    card = build_card(_clean_record())
    assert card["topology"] == {
        "graph_node_names": ["fetch", "reply"],
        "graph_edge_map": {"fetch": ["reply"], "reply": []},
    }


def test_a_step_carries_node_status_superstep_duration_and_update_shape():
    record = _clean_record()
    record.steps[0].superstep = "#2"
    step = build_card(record)["steps"][0]
    assert step["node"] == "fetch" and step["status"] == "pass"
    assert step["superstep"] == "#2" and step["duration_ms"] == 12.5
    assert step["update"] == {
        "t": "object",
        "len": 1,
        "keys": {"docs": {"t": "array", "len": 2, "item": {"t": "string", "len": 1}}},
    }
    assert build_card(record)["steps"][1]["superstep"] is None


def test_each_top_level_update_key_gets_its_shape_and_no_value():
    record = make_run_record(
        events=[
            make_event(
                output={
                    "docs": [],
                    "summary": "Your refund has been scheduled.",
                    "ok": False,
                    "count": 3,
                    "ratio": 0.5,
                    "nothing": None,
                    "customer": {"email": "ana@acme.com"},
                }
            )
        ]
    )
    keys = build_card(record)["steps"][0]["update"]["keys"]
    assert keys == {
        "docs": {"t": "array", "len": 0},
        "summary": {"t": "string", "len": 31, "f": ["done_word"]},
        "ok": {"t": "bool"},
        "count": {"t": "int"},
        "ratio": {"t": "float"},
        "nothing": {"t": "null"},
        "customer": {"t": "object", "len": 1},
    }


def test_an_empty_update_is_visible_and_a_missing_update_is_null():
    card = build_card(make_run_record(events=[make_event(output={}), make_event(output=None)]))
    assert card["steps"][0]["update"] == {"t": "object", "len": 0, "keys": {}}
    assert card["steps"][1]["update"] is None


def test_keys_that_are_not_identifiers_share_one_question_mark_and_len_counts_them_all():
    record = make_run_record(events=[make_event(output={"a": 1, CANARY: "x", "user-id": 2})])
    update = build_card(record)["steps"][0]["update"]
    assert update["len"] == 3
    assert sorted(update["keys"]) == ["?", "a"]


def test_every_string_feature_fits_the_schema():
    samples = [
        "",
        "line one\nline two",
        '{"a": 1}',
        "https://acme.example/x",
        "ana@acme.example",
        "42.5",
        "Your refund has been scheduled.",
        "status: requires_approval",
        "Error: 404 not found",
        "I'm unable to help with that.",
        "Lorem ipsum dolor",
    ]
    record = make_run_record(
        events=[make_event(output={f"s{i}": v for i, v in enumerate(samples)})]
    )
    card = build_card(record)
    validate(card, SCHEMA)
    seen = {
        f for shaped in card["steps"][0]["update"]["keys"].values() for f in shaped.get("f", [])
    }
    assert seen == set(SCHEMA["$defs"]["shape"]["properties"]["f"]["items"]["enum"])


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            {"name": "crm", "output": None, "error": "HTTPError: 503 Service Unavailable"},
            {"status": 503, "failure_class": "transient", "error": True},
        ),
        (
            {"name": "crm", "output": None, "error": "ReadTimeout: timed out"},
            {"status": None, "failure_class": "transient", "error": True},
        ),
        (
            {"name": "crm", "output": None, "error": "ValueError: bad input"},
            {"status": None, "failure_class": "fatal", "error": True},
        ),
        (
            {"name": "crm", "output": {"status_code": 429}, "error": None},
            {"status": 429, "failure_class": "rate_limited", "error": False},
        ),
        (
            {"name": "crm", "output": {"status": 404, "body": "gone"}, "error": None},
            {"status": 404, "failure_class": "fatal", "error": False},
        ),
        (
            {"name": "crm", "output": '{"status_code": 502}', "error": None},
            {"status": 502, "failure_class": "transient", "error": False},
        ),
        (
            {"name": "crm", "output": {"status": 200}, "error": None},
            {"status": 200, "failure_class": None, "error": False},
        ),
        (
            {"name": "crm", "output": "plain text answer", "error": None},
            {"status": None, "failure_class": None, "error": False},
        ),
        (
            {"name": "crm", "input": "q", "output": {"rows": []}, "error": None},
            {"status": None, "failure_class": None, "error": False},
        ),
    ],
)
def test_a_tool_call_is_its_name_status_failure_class_and_error_flag(call, expected):
    event = make_event(output={"x": 1})
    event.tool_calls = [call]
    (tool,) = build_card(make_run_record(events=[event]))["steps"][0]["tools"]
    assert tool == {"name": "crm", **expected}


@pytest.mark.parametrize(
    "output",
    [{"status": "ok"}, {"code": 7}, {"status": True}, {"status": 99999}, {"status": 42}, []],
)
def test_a_value_that_is_not_an_http_status_is_not_reported_as_one(output):
    event = make_event(output={"x": 1})
    event.tool_calls = [{"name": "crm", "output": output, "error": None}]
    (tool,) = build_card(make_run_record(events=[event]))["steps"][0]["tools"]
    assert tool["status"] is None and tool["failure_class"] is None


def test_a_tool_call_carries_nothing_of_its_input_output_or_error_text():
    event = make_event(output={"x": 1})
    event.tool_calls = [
        {
            "name": "crm",
            "input": CANARY,
            "output": {"status": 500, "body": CANARY},
            "error": CANARY,
        }
    ]
    (tool,) = build_card(make_run_record(events=[event]))["steps"][0]["tools"]
    assert set(tool) == {"name", "status", "failure_class", "error"}
    assert CANARY not in json.dumps(tool)


def test_a_finding_carries_the_allowlisted_fields_and_no_reason():
    record = _failing_record()
    record.findings = [
        Finding(
            id="x",
            node="fetch",
            type="error_response",
            severity="critical",
            reason="HTTP 503 from crm; the customer is ana@acme.com",
            source="heuristic",
            field_path="crm.status",
            origin_node="fetch",
            confidence=0.85,
            suppressed=True,
            failure_class="transient",
        )
    ]
    (finding,) = build_card(record)["findings"]
    assert set(finding) == {
        "id",
        "type",
        "node",
        "field_path",
        "origin_node",
        "severity",
        "source",
        "confidence",
        "failure_class",
        "suppressed",
    }
    assert finding | {"id": None} == {
        "id": None,
        "type": "error_response",
        "node": "fetch",
        "field_path": "crm.status",
        "origin_node": "fetch",
        "severity": "critical",
        "source": "heuristic",
        "confidence": 0.85,
        "failure_class": "transient",
        "suppressed": True,
    }
    assert "ana@acme.com" not in json.dumps(finding)


def test_a_findings_id_is_the_local_one_when_nothing_was_masked():
    record = _failing_record()
    card = build_card(record)
    assert [f["id"] for f in card["findings"]] == [f.id for f in record.findings]


def test_a_findings_id_is_recomputed_from_the_masked_path():
    # The local id is a hash of the raw path: it would let anyone test a guess at the value.
    record = _failing_record()
    raw = f"customer.{CANARY}.balance"
    record.findings = [
        Finding(
            id="local",
            node="fetch",
            type="sentinel_value",
            severity="critical",
            reason="r",
            source="heuristic",
            field_path=raw,
        )
    ]
    (finding,) = build_card(record)["findings"]
    assert finding["field_path"] == "customer.?.balance"
    assert finding["id"] == _fid("sentinel_value", "fetch", "customer.?.balance", "heuristic")
    assert finding["id"] != _fid("sentinel_value", "fetch", raw, "heuristic")


@pytest.mark.parametrize(
    ("raw", "sent"),
    [
        ("empty_result", "empty_result"),
        ("BA-001", "BA-001"),
        ("review_wrong_total", "review_wrong_total"),
        ("review_Total mismatch: $4,512.00", "other"),
        (f"Saw {CANARY}", "other"),
        (f"review_{CANARY}", "other"),  # an LLM-chosen review kind is free text
        ("PH-0011", "other"),
        ("ssn-123", "other"),
        ("x" * 49, "other"),
        ("", "other"),
    ],
)
def test_a_free_text_finding_type_collapses_to_other(raw, sent):
    record = _failing_record()
    record.findings = [
        Finding(id="x", node="fetch", type=raw, severity="warning", reason="r", source="llm")
    ]
    assert build_card(record)["findings"][0]["type"] == sent


def test_a_findings_confidence_is_a_finite_number_or_null():
    record = _failing_record()
    record.findings = [
        Finding(
            id=str(i), node="n", type="t", severity="info", reason="r", source="llm", confidence=c
        )
        for i, c in enumerate([0.5, None, float("nan"), float("inf")])
    ]
    assert [f["confidence"] for f in build_card(record)["findings"]] == [0.5, None, None, None]


def test_llm_usage_is_token_and_call_counts_per_model_and_nothing_else():
    record = _clean_record()
    record.steps[1].llm_usage = LLMUsage(
        calls=[
            LLMCallInfo("gpt-4o", 20, 10, 30, output_text=CANARY),
            LLMCallInfo("gemini-flash", 1, 2, 3, cost_usd=0.5),
        ]
    )
    assert build_card(record)["llm_usage"] == [
        {
            "model": "gemini-flash",
            "calls": 1,
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
        },
        {
            "model": "gpt-4o",
            "calls": 2,
            "prompt_tokens": 30,
            "completion_tokens": 15,
            "total_tokens": 45,
        },
    ]
    assert CANARY not in json.dumps(build_card(record))


def test_a_model_name_that_is_not_a_model_name_is_sent_as_other():
    record = _clean_record()
    record.steps[0].llm_usage = LLMUsage(calls=[LLMCallInfo(f"my {CANARY} model", 1, 1, 2)])
    assert build_card(record)["llm_usage"][0]["model"] == "other"


def test_root_cause_chain_is_carried():
    assert build_card(_crashed_record())["root_cause_chain"] == ["fetch", "reply"]


def test_the_fix_prompt_is_the_sanitized_one_for_a_failing_run():
    record = _failing_record()
    card = build_card(record)
    assert card["fix_prompt"] == build_fix_prompt_for_record(record, sanitized=True).prompt
    assert "Values omitted" in card["fix_prompt"]


def test_a_clean_run_has_no_fix_prompt():
    assert build_card(_clean_record())["fix_prompt"] is None


def test_a_failed_run_with_nothing_to_write_a_prompt_about_has_none():
    record = make_run_record(events=[make_event()], status="silent_failure")
    assert build_card(record)["fix_prompt"] is None


def test_a_fix_prompt_that_cannot_be_built_costs_the_card_only_the_prompt(caplog):
    record = _failing_record()
    record.steps[0].output_dict = "not a dict"  # type: ignore[assignment]  # a hand-edited run file
    with caplog.at_level("WARNING", logger="argus"):
        card = build_card(record)
    validate(card, SCHEMA)
    assert card["fix_prompt"] is None
    assert card["findings"] and card["steps"][0]["update"] is None
    # the log names the exception class and nothing the exception said: that can quote a value
    assert [r.getMessage() for r in caplog.records] == [
        "argus: no fix prompt in the run card (AttributeError)"
    ]


def test_building_a_card_is_deterministic_and_leaves_the_record_alone():
    from argus.storage import _to_json_serializable

    record = _failing_record()
    before = json.dumps(_to_json_serializable(record), sort_keys=True)
    assert build_card(record) == build_card(record)
    assert json.dumps(_to_json_serializable(record), sort_keys=True) == before


def test_a_huge_run_is_capped_and_still_validates():
    events = []
    for i in range(cloud_card.MAX_STEPS + 20):
        event = make_event(
            node_name=f"n{i % 700}",
            step_index=i,
            output={f"k{j}": j for j in range(cloud_card.MAX_KEYS + 30)},
        )
        event.tool_calls = [
            {"name": f"t{j}", "output": None, "error": None}
            for j in range(cloud_card.MAX_TOOLS + 9)
        ]
        events.append(event)
    record = make_run_record(events=events, edges={f"n{i}": ["x"] * 150 for i in range(700)})
    record.graph_node_names = [f"n{i}" for i in range(700)]
    record.findings = [
        Finding(id=str(i), node="n0", type="t", severity="info", reason="r", source="llm")
        for i in range(cloud_card.MAX_FINDINGS + 5)
    ]
    record.root_cause_chain = [f"n{i}" for i in range(cloud_card.MAX_CHAIN + 5)]
    card = build_card(record)
    validate(card, SCHEMA)
    assert len(card["steps"]) == cloud_card.MAX_STEPS
    assert len(card["findings"]) == cloud_card.MAX_FINDINGS
    assert len(card["steps"][0]["tools"]) == cloud_card.MAX_TOOLS
    assert len(card["steps"][0]["update"]["keys"]) == cloud_card.MAX_KEYS
    assert card["steps"][0]["update"]["len"] == cloud_card.MAX_KEYS + 30
    assert len(card["topology"]["graph_node_names"]) == cloud_card.MAX_NODES


def test_a_record_with_odd_values_still_builds_a_valid_card():
    odd = make_event(output=["not", "a", "dict"], duration_ms=float("nan"))
    odd.tool_calls = ["not a dict", {"name": None, "output": object(), "error": None}]
    odd.superstep = "x" * 999
    record = make_run_record(events=[odd], edges={"a": []})
    record.started_at = "yesterday-ish"
    record.argus_version = "not a version!"
    record.duration_ms = float("inf")
    record.agent = "A" * 500
    card = build_card(record)
    validate(card, SCHEMA)
    assert card["run"]["started_at"] is None and card["run"]["argus_version"] == "unknown"
    assert card["run"]["duration_ms"] is None and len(card["run"]["agent"]) == cloud_card.MAX_NAME
    assert card["steps"][0]["update"] is None and card["steps"][0]["duration_ms"] == 0.0
    assert card["steps"][0]["tools"] == [
        {"name": "tool", "status": None, "failure_class": None, "error": False}
    ]


def test_a_run_file_with_null_lists_still_builds_a_valid_card():
    event = make_event(output={"x": 1})
    event.tool_calls = None  # type: ignore[assignment]
    record = make_run_record(events=[event])
    record.graph_node_names = None  # type: ignore[assignment]
    record.root_cause_chain = None  # type: ignore[assignment]
    record.findings = None  # type: ignore[assignment]
    record.graph_edge_map = {"a": None}  # type: ignore[dict-item]
    card = build_card(record)
    validate(card, SCHEMA)
    assert card["steps"][0]["tools"] == [] and card["findings"] == []
    assert card["topology"] == {"graph_node_names": [], "graph_edge_map": {"a": []}}


# ── a real pipeline: a clean run, a failing run and a crashed run ────────────


class _State(TypedDict, total=False):
    n: int
    docs: list
    answer: str


def _recorded(build):
    pytest.importorskip("langgraph")
    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder
    from argus.storage import load_run

    graph = StateGraph(_State)
    for name, fn in build.items():
        graph.add_node(name, fn)
    names = list(build)
    graph.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        graph.add_edge(a, b)
    graph.add_edge(names[-1], END)
    recorder = ArgusRecorder(semantic_judge=False, agent="demo-agent")
    try:
        recorder.attach(graph.compile()).invoke({"n": 0})
    except Exception:  # the crashed run is the point
        pass
    return load_run(recorder.run_ids[-1])


@pytest.mark.integration
def test_real_clean_failing_and_crashed_runs_produce_valid_cards():
    clean = _recorded({"fetch": lambda s: {"docs": ["a"]}, "reply": lambda s: {"answer": "hi"}})
    failing = _recorded({"fetch": lambda s: {}, "reply": lambda s: {"answer": "hi"}})

    def crash(state):
        raise KeyError("summary")

    crashed = _recorded({"fetch": lambda s: {"docs": ["a"]}, "reply": crash})

    statuses = []
    for record in (clean, failing, crashed):
        card = build_card(record)
        validate(card, SCHEMA)
        assert card["run"]["agent"] == "demo-agent" and card["run"]["environment"]
        statuses.append(card["run"]["overall_status"])
    assert statuses == ["clean", "silent_failure", "crashed"]
    assert build_card(clean)["fix_prompt"] is None
    assert build_card(failing)["fix_prompt"] and build_card(crashed)["fix_prompt"]
