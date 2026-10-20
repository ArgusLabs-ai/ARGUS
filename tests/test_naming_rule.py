"""The naming rule (``argus.utils.names``) and the one value-to-shape function.

The run card and ``argus fix --sanitized`` both name state keys and field paths by this rule: shown
only when they look like an identifier, otherwise ``?``. The card and telemetry both turn values
into shapes with ``telemetry.card.value_shape``; only the key handling differs.
"""

from __future__ import annotations

import json

import pytest

from argus.telemetry.card import _h, shape, value_shape
from argus.utils import names

CANARY = "CANARY-SSN-123-45-6789"
SECRET = "test-secret"


# ── naming rule ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("key", ["docs", "_private", "a", "A1_b2", "x" * 64])
def test_identifier_keys_go_in_clear(key):
    assert names.safe_key(key) == key


@pytest.mark.unit
@pytest.mark.parametrize(
    "key", ["", "1st", "user-id", "a b", "a.b", "é", "x" * 65, CANARY, "ok\n", "a[0]", "?"]
)
def test_any_other_key_becomes_a_question_mark(key):
    assert names.safe_key(key) == "?"


@pytest.mark.unit
@pytest.mark.parametrize("key", [7, None, ("a", "b"), b"docs", object()])
def test_a_key_that_is_not_text_is_never_named(key):
    assert names.safe_key(key) == "?"


# A path is named from the data it points into: a segment is shown only if it is a real key (or
# list index) at its level and looks like an identifier. Dots are how a path is written, so a
# key that contains one ("john.smith") cannot be told from two nested keys; the data can.
ACCOUNTS = {
    "accounts": {"john.smith": {"balance": -1}, "ana": {"balance": 2}},
    "items": [{"id": 1, "name": "x"}],
    "123-45-6789": {"balance": 3},
    "customer": {CANARY: {"balance": 4}},
    "result": {"items": [{"summary": "s"}]},
    "john": 1,
    "john.smith": {"x": 1},
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("accounts", "accounts"),
        ("accounts.ana", "accounts.ana"),
        ("accounts.ana.balance", "accounts.ana.balance"),
        ("items[0].name", "items[0].name"),
        ("items[0]", "items[0]"),
        ("result.items.[0].summary", "result.items.[0].summary"),  # SemanticSignal.dotted_path
        (f"customer.{CANARY}.balance", "customer.?.balance"),  # a real key, but not a name
        ("123-45-6789.balance", "?.balance"),
        ("accounts.nobody.balance", "accounts.?.?"),  # not a key: nothing after it is trusted
        ("items[1].id", "?.?"),  # past the end of the list
        ("items.name", "items.?"),  # a list has no key
        ("items[0].nope.x", "items[0].?.?"),
        ("accounts.ana.balance.deeper", "accounts.ana.balance.?"),  # below a leaf
        ("a.b.c.d.e.f.g.h.i.j", "?.?.?.?.?.?.?.?"),  # at most eight segments
    ],
)
def test_each_path_segment_must_be_a_name_or_a_real_key(path, expected):
    assert names.safe_path(path, ACCOUNTS) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("accounts.john.smith.balance", "accounts.?.?.?"),
        ("accounts.john.smith", "accounts.?.?"),
        ("accounts.john", "accounts.?"),
        ("john.smith.x", "?.?.?"),  # "john" sits beside "john.smith": ambiguous
        ("john.smith", "?.?"),
    ],
)
def test_a_key_that_contains_a_dot_is_never_shown_as_its_parts(path, expected):
    assert names.safe_path(path, ACCOUNTS) == expected


@pytest.mark.unit
def test_a_dotted_top_level_key_hides_its_first_part_too():
    world = {"john.smith": {"balance": 1}}
    assert names.safe_path("john.smith.balance", world) == "?.?.?"
    assert names.safe_path("john.smith", world) == "?.?"
    # also when the key is not in the data at all (a baseline names the key a node stopped writing)
    assert names.safe_path("john.smith", {"docs": []}) == "?.?"
    assert names.safe_path("john.smith") == "?.?"


@pytest.mark.unit
def test_a_first_segment_the_data_does_not_hold_is_shown_only_as_the_whole_path():
    # A field nobody wrote (missing_fields) is in no data; it is a name from the code.
    assert names.safe_path("summary", {"docs": []}) == "summary"
    assert names.safe_path("docs[0]", {"other": 1}) == "docs[0]"
    # With more segments after it, it could be the start of a dotted key, so it is not shown.
    assert names.safe_path("email.body", {"docs": []}) == "?.?"
    assert names.safe_path("user-id", {"docs": []}) == "?"
    assert names.safe_path("*:judge@3", {"docs": []}) == "?"
    assert names.safe_path("summarize:my_fn") == "?"


@pytest.mark.unit
def test_without_data_only_a_one_segment_path_can_be_named():
    assert names.safe_path("customer") == "customer"
    assert names.safe_path("customer.balance") == "?.?"
    assert names.safe_path("items[0].name") == "?.?"
    assert names.safe_path(f"{CANARY}") == "?"


@pytest.mark.unit
def test_a_long_path_through_real_keys_keeps_its_first_eight_segments():
    deep: object = {"leaf": 1}
    for key in "ihgfedcba":
        deep = {key: deep}
    assert names.safe_path("a.b.c.d.e.f.g.h.i.leaf", deep) == "a.b.c.d.e.f.g.h"


@pytest.mark.unit
def test_every_root_is_consulted_and_a_segment_needs_only_one_to_hold_it():
    update = {"customer": {"email": "x"}}
    state = {"customer": {"plan": "y"}, "docs": [{"id": 1}]}
    assert names.safe_path("customer.email", update, state) == "customer.email"
    assert names.safe_path("customer.plan", update, state) == "customer.plan"
    assert names.safe_path("customer.nope", update, state) == "customer.?"
    assert names.safe_path("docs[0].id", update, state) == "docs[0].id"
    # roots that are not containers are ignored, not an error
    assert names.safe_path("customer.email", None, "text", 7, update) == "customer.email"


@pytest.mark.unit
@pytest.mark.parametrize("path", [None, ""])
def test_no_path_stays_no_path(path):
    assert names.safe_path(path) is None
    assert names.safe_path(path, ACCOUNTS) is None


@pytest.mark.unit
def test_a_path_that_is_not_text_is_judged_by_its_text():
    assert names.safe_path(7) == "?"
    assert names.safe_path(7, {"7": 1}) == "?"


@pytest.mark.unit
def test_an_empty_segment_is_never_a_name():
    assert names.safe_path("a..b", {"a": {"b": 1}}) == "a.?.?"
    assert names.safe_path("a.", {"a": {"b": 1}}) == "a.?"


# ── the data a step's paths point into ───────────────────────────────────────


class _Step:
    def __init__(self, output_dict=None, input_state=None, tool_calls=None):
        self.output_dict, self.input_state, self.tool_calls = output_dict, input_state, tool_calls


@pytest.mark.unit
def test_a_steps_roots_are_its_update_its_input_and_each_tool_output_under_the_tools_name():
    step = _Step(
        {"a": 1},
        {"b": 2},
        [
            {"name": "crm", "output": {"status": 503, "data": {"id": 1}}},
            {"name": "mail", "output": '{"sent": false}'},  # JSON text, as the inspector reads it
            {"name": "search", "output": [{"id": 1}]},  # a list is read as {"results": [...]}
            {"name": "plain", "output": "just text"},
            {"output": {"x": 1}},  # no name: the inspector calls it "tool"
            "not a call",
        ],
    )
    roots = names.step_roots(step)
    path = lambda p: names.safe_path(p, *roots)  # noqa: E731
    assert path("a") == "a" and path("b") == "b"
    assert path("crm.status") == "crm.status" and path("crm.data.id") == "crm.data.id"
    assert path("mail.sent") == "mail.sent"
    assert path("search.results[0].id") == "search.results[0].id"
    assert path("plain.x") == "plain.?"
    assert path("tool.x") == "tool.x"
    assert path("crm.other") == "crm.?"


@pytest.mark.unit
def test_a_step_with_nothing_recorded_has_no_roots_to_trust():
    assert names.safe_path("a.b", *names.step_roots(_Step())) == "?.?"
    assert names.safe_path("a.b", *names.step_roots(object())) == "?.?"
    assert names.safe_path("a", *names.step_roots(object())) == "a"


# ── one value-to-shape function, two callers ─────────────────────────────────


@pytest.mark.unit
def test_without_a_name_function_no_nested_key_is_sent():
    assert value_shape({"a": {CANARY: "x"}}, None, False) == {"t": "object", "len": 1}
    assert value_shape([{CANARY: 1}, 2], None, False) == {
        "t": "array",
        "len": 2,
        "item": {"t": "object", "len": 1},
    }


@pytest.mark.unit
def test_a_boolean_carries_its_value_only_when_asked():
    assert value_shape(True, None, False) == {"t": "bool"}
    assert value_shape(False, None, True) == {"t": "bool", "v": False}


@pytest.mark.unit
def test_strings_and_scalars_keep_type_length_and_features_in_both_modes():
    for bools in (True, False):
        assert value_shape(3, None, bools) == {"t": "int"}
        assert value_shape(3.5, None, bools) == {"t": "float"}
        assert value_shape(None, None, bools) == {"t": "null"}
        assert value_shape("Error: 404 not found", None, bools) == {
            "t": "string",
            "len": 20,
            "f": ["error_word"],
        }


@pytest.mark.unit
def test_a_name_function_names_every_key_at_every_depth():
    shaped = value_shape({"a": {"b": 1}}, lambda key: f"<{key}>", True)
    assert shaped == {
        "t": "object",
        "keys": {"<a>": {"t": "object", "keys": {"<b>": {"t": "int"}}}},
    }


@pytest.mark.unit
def test_telemetry_shape_is_the_hashed_flavour_of_the_same_function():
    value = {"customer": {"email": "ana@acme.com", "ok": False}, "rows": [{"id": 1}]}
    assert shape(value, SECRET) == value_shape(value, lambda key: _h(key, SECRET), True)


@pytest.mark.unit
def test_the_shape_is_bounded_in_both_modes():
    nested_lists: object = "leaf"
    nested_dicts: object = "leaf"
    for _ in range(20):
        nested_lists = [nested_lists]
        nested_dicts = {"k": nested_dicts}
    assert "truncated" in json.dumps(value_shape(nested_lists, None, False))
    assert "truncated" in json.dumps(value_shape(nested_dicts, lambda key: key, True))


@pytest.mark.unit
def test_roots_are_read_one_at_a_time_and_a_dotted_key_in_any_of_them_caps_the_path():
    dotted = {"accounts": {"john.smith": {"balance": 1}}}
    nested = {"accounts": {"john": {"smith": {"balance": 2}}}}
    assert names.safe_path("accounts.john.smith.balance", nested) == "accounts.john.smith.balance"
    assert names.safe_path("accounts.john.smith.balance", dotted, nested) == "accounts.?.?.?"
    assert names.safe_path("accounts.john.smith.balance", nested, dotted) == "accounts.?.?.?"
    # no clash, no cap: another root merely lacking a key does not hide it
    other = {"accounts": {"ana": 1}}
    assert names.safe_path("accounts.john.smith.balance", other, nested) == (
        "accounts.john.smith.balance"
    )
