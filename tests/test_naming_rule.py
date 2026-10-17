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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("customer", "customer"),
        ("customer.balance", "customer.balance"),
        ("items[0].name", "items[0].name"),
        ("items[0][12]", "items[0][12]"),
        ("result.items.[0].summary", "result.items.[0].summary"),  # SemanticSignal.dotted_path
        (f"customer.{CANARY}.balance", "customer.?.balance"),
        (f"{CANARY}", "?"),
        ("a..b", "a.?.b"),
        ("a.", "a.?"),
        ("*:judge@3", "?"),
        ("summarize:my_fn", "?"),
        ("a[x]", "?"),  # a bracket that is not an index is a name
        ("a[0]x", "?"),
        ("a[1234567]", "?"),
        ("a.b.c.d.e.f.g.h.i.j", "a.b.c.d.e.f.g.h"),  # at most eight segments
    ],
)
def test_each_path_segment_is_judged_on_its_own(path, expected):
    assert names.safe_path(path) == expected


@pytest.mark.unit
@pytest.mark.parametrize("path", [None, ""])
def test_no_path_stays_no_path(path):
    assert names.safe_path(path) is None


@pytest.mark.unit
def test_a_path_that_is_not_text_is_judged_by_its_text():
    assert names.safe_path(7) == "?"


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
