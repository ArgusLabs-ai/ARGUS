"""Shape card: what automatic telemetry sends about a run (docs/DATA.md).

The card is built by allowlist from named fields. These tests plant canary
strings in every place a pipeline value, a name or a model's prose can live,
and fail if any of them reaches the card.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

import pytest
from conftest import make_event, make_run_record

from argus.models import Finding
from argus.telemetry.card import build_card, shape

SECRET = "test-secret"
CANARY = "CANARY-SSN-123-45-6789"
CANARY_KEY = "canary_customer_ssn"
CANARY_NODE = "canary_lookup_node"
CANARY_TOOL = "canary_crm_tool"
SCHEMA = json.loads(
    (Path(__file__).parent.parent / "src/argus/data/share_card.schema.json").read_text()
)


def _card(record, **kw):
    return build_card(record, secret=SECRET, install_id="i_test", **kw)


def _leaky_record():
    """A failed run with a canary in every value, name, reason and claim."""
    step = make_event(
        node_name=CANARY_NODE,
        status="fail",
        input_state={CANARY_KEY: CANARY},
        output={CANARY_KEY: CANARY, "nested": {"deep": [CANARY, {CANARY_KEY: CANARY}]}},
        exception=f"KeyError: {CANARY}",
    )
    step.tool_calls = [
        {
            "name": CANARY_TOOL,
            "input": {"q": CANARY},
            "output": {"rows": [CANARY]},
            "error": CANARY,
        }
    ]
    step.review = [{"kind": "wrong_total", "claim": CANARY, "why": CANARY, "role": "confirms"}]
    record = make_run_record(events=[step], edges={CANARY_NODE: ["__end__"]}, status="fail")
    record.initial_state = {CANARY_KEY: CANARY}
    record.root_cause_chain = [CANARY_NODE]
    record.findings = [
        Finding(
            id="f1",
            node=CANARY_NODE,
            type="empty_result",
            severity="critical",
            reason=f"{CANARY_NODE} returned {CANARY}",
            source="heuristic",
            field_path=f"{CANARY_KEY}.inner",
            origin_node=CANARY_NODE,
        ),
        # A learned-signature category or an LLM review kind is free text.
        Finding(
            id="f2",
            node=CANARY_NODE,
            type=f"Saw {CANARY}",
            severity="warning",
            reason=CANARY,
            source="llm",
        ),
    ]
    return record


# ── privacy ───────────────────────────────────────────────────────────────────


def test_no_value_name_or_prose_reaches_the_card():
    payload = json.dumps(_card(_leaky_record()))
    for canary in (CANARY, CANARY_KEY, CANARY_NODE, CANARY_TOOL, "canary", "123-45"):
        assert canary.lower() not in payload.lower()


def test_free_text_rule_names_collapse_to_other():
    types = [f["type"] for f in _card(_leaky_record())["findings"]]
    assert types == ["empty_result", "other"]


def test_names_hash_stably_per_secret_and_differ_across_installs():
    record = _leaky_record()
    a, b = _card(record), _card(record)
    other = build_card(record, secret="another-install", install_id="i2")
    assert a["steps"][0]["node"] == b["steps"][0]["node"]
    assert a["fingerprint"] == b["fingerprint"]
    assert a["steps"][0]["node"] != other["steps"][0]["node"]


def test_run_id_is_hashed():
    card = _card(make_run_record(run_id="customer-acme-run-7"))
    assert "acme" not in json.dumps(card)
    assert card["run"].startswith("r_")


# ── what it does carry ───────────────────────────────────────────────────────


def test_card_carries_verdict_rules_and_blame():
    card = _card(_leaky_record())
    node = card["steps"][0]["node"]
    assert card["status"] == "fail"
    assert card["blamed"] == [node]
    first = card["findings"][0]
    assert first["node"] == node and first["origin"] == node
    assert first["severity"] == "critical" and first["source"] == "heuristic"
    assert first["heuristic"] is False
    assert card["steps"][0]["error"] is True
    assert card["steps"][0]["tools"][0]["error"] is True
    assert card["steps"][0]["review"] == [{"role": "confirms", "kind": "wrong_total"}]


def test_heuristic_rules_are_marked():
    record = make_run_record(events=[make_event()])
    record.findings = [
        Finding(
            id="x",
            node="node_a",
            type="status_overstated",
            severity="critical",
            reason="r",
            source="heuristic",
        )
    ]
    assert _card(record)["findings"][0]["heuristic"] is True


def test_dotted_field_path_is_hashed_per_segment():
    card = _card(_leaky_record())
    field = card["findings"][0]["field"]
    assert field.count(".") == 1 and all(p.startswith("k_") for p in field.split("."))


def test_graph_summary_and_fingerprint():
    a = make_event(node_name="a")
    b = make_event(node_name="b", step_index=1)
    card = _card(make_run_record(events=[a, b], edges={"a": ["b"], "b": ["__end__"]}))
    assert card["graph"] == {"nodes": 2, "edges": 2, "cyclic": False}
    assert card["fingerprint"].startswith("g_")


def test_ci_flag_and_versions():
    card = _card(make_run_record(), ci=True)
    assert card["ci"] is True
    assert card["schema"] == 1
    assert card["argus_version"] == "0.8.11"
    assert card["install"] == "i_test"


# ── shape ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "features"),
    [
        ("", ["empty"]),
        ("   ", ["empty"]),
        ("Your refund has been scheduled.", ["done_word"]),
        ("status: requires_approval", ["pending_word"]),
        ("Error: 404 not found", ["error_word"]),
        ("I'm unable to help with that.", ["refusal"]),
        ("Lorem ipsum dolor", ["placeholder"]),
        ('{"a": 1}', ["json_like"]),
        ("https://acme.example/x", ["url"]),
        ("ana@acme.example", ["email_like"]),
        ("42.5", ["numeric"]),
        ("line one\nline two", ["multiline"]),
    ],
)
def test_string_features(value, features):
    assert shape(value, SECRET)["f"] == features


def test_shape_of_each_type():
    assert shape(None, SECRET) == {"t": "null"}
    assert shape(True, SECRET) == {"t": "bool", "v": True}
    assert shape(3, SECRET) == {"t": "int"}
    assert shape(3.5, SECRET) == {"t": "float"}
    assert shape("abc", SECRET) == {"t": "string", "len": 3}
    assert shape([1, 2], SECRET) == {"t": "array", "len": 2, "item": {"t": "int"}}
    assert shape([], SECRET) == {"t": "array", "len": 0}
    obj = shape({"ok": False}, SECRET)
    assert obj["t"] == "object" and list(obj["keys"].values()) == [{"t": "bool", "v": False}]


def test_shape_is_bounded():
    deep: Any = "leaf"
    for _ in range(20):
        deep = {"k": deep}
    assert "truncated" in json.dumps(shape(deep, SECRET))
    wide = shape({f"k{i}": i for i in range(200)}, SECRET)
    assert len(wide["keys"]) == 50 and wide["more"] == 150


# ── schema ──────────────────────────────────────────────────────────────────


def _validate(value, schema, path="$"):
    """Enough JSON Schema for this file: type, properties, required,
    additionalProperties, items, enum, $ref. The server runs a full validator."""
    if "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].rsplit("/", 1)[1]]
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        py = {
            "object": dict,
            "array": list,
            "string": str,
            "boolean": bool,
            "integer": int,
            "number": (int, float),
            "null": type(None),
        }
        ok = any(
            isinstance(value, py[t])
            and not (t in ("integer", "number") and isinstance(value, bool))
            for t in types
        )
        assert ok, f"{path}: {value!r} is not {types}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} not in enum"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            assert key in value, f"{path}: missing {key}"
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{path}.{key}")
            else:
                extra = schema.get("additionalProperties", True)
                assert extra is not False, f"{path}: unknown field {key}"
                if isinstance(extra, dict):
                    _validate(item, extra, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]")


def test_card_matches_published_schema():
    _validate(_card(_leaky_record()), SCHEMA)
    _validate(_card(make_run_record(), ci=True), SCHEMA)


def test_schema_rejects_unknown_fields():
    card = _card(make_run_record())
    card["note"] = "anything"
    with pytest.raises(AssertionError, match="unknown field"):
        _validate(card, SCHEMA)


# ── a real pipeline ─────────────────────────────────────────────────────────


@pytest.mark.integration
def test_real_langgraph_run_leaks_nothing(monkeypatch):
    pytest.importorskip("langgraph")
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder
    from argus.storage import load_run

    monkeypatch.setattr("argus.patcher.patch_graph", lambda *a, **k: 1 / 0)

    class State(TypedDict, total=False):
        canary_customer_ssn: str
        canary_results: list

    @tool
    def canary_crm_tool(q: str) -> dict:
        """Look up a customer."""
        return {"results": [], "echo": q}

    def canary_lookup_node(state: State) -> dict:
        canary_crm_tool.invoke({"q": state["canary_customer_ssn"]})
        return {"canary_results": []}

    def canary_reply_node(state: State) -> dict:
        return {"canary_customer_ssn": f"Done for {CANARY}"}

    g = StateGraph(State)
    g.add_node("canary_lookup_node", canary_lookup_node)
    g.add_node("canary_reply_node", canary_reply_node)
    g.add_edge(START, "canary_lookup_node")
    g.add_edge("canary_lookup_node", "canary_reply_node")
    g.add_edge("canary_reply_node", END)

    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(g.compile()).invoke({"canary_customer_ssn": CANARY})
    card = _card(load_run(recorder.session.run_id))

    _validate(card, SCHEMA)
    assert len(card["steps"]) == 2
    assert "canary" not in json.dumps(card).lower()
    assert "123-45" not in json.dumps(card)
