"""Unit tests for the state patch layer (time-travel replay, part 1)."""

import copy

import pytest

from argus.state_patch import (
    PatchError,
    apply_patch,
    format_changes,
    parse_path,
    preview_patch,
    validate_patch,
)


@pytest.fixture
def state():
    return {
        "query": "original query",
        "items": [{"name": "a", "score": 1}, {"name": "b", "score": 2}],
        "meta": {"retries": 3, "model": "gpt-4o", "nested": {"deep": True}},
        "broken_field": "PLACEHOLDER",
        "count": 7,
    }


# ── Path parsing ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,expected",
    [
        ("query", ["query"]),
        ("meta.retries", ["meta", "retries"]),
        ("items[0]", ["items", 0]),
        ("items.[0]", ["items", 0]),  # argus dotted_path form
        ("items[0].name", ["items", 0, "name"]),
        ("items.[0].name", ["items", 0, "name"]),
        ("matrix[1][2]", ["matrix", 1, 2]),
        ("[0]", [0]),
        ("items[-1]", ["items", -1]),
    ],
)
def test_parse_path_forms(path, expected):
    assert parse_path(path) == expected


@pytest.mark.unit
def test_bracket_and_dotted_index_forms_are_equivalent():
    assert parse_path("a.b[0].c") == parse_path("a.b.[0].c")


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["", "   ", "a..b", ".a", "a.", "a[x]", "a[", "a]", "a[0", "a.[]"]
)
def test_parse_path_rejects_malformed(bad):
    with pytest.raises(PatchError):
        parse_path(bad)


@pytest.mark.unit
def test_parse_path_rejects_non_string():
    with pytest.raises(PatchError, match="must be a string"):
        parse_path(42)


# ── set ───────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_set_top_level(state):
    out = apply_patch(state, {"set": {"query": "fixed"}})
    assert out["query"] == "fixed"


@pytest.mark.unit
def test_set_nested(state):
    out = apply_patch(state, {"set": {"meta.retries": 0}})
    assert out["meta"]["retries"] == 0
    assert out["meta"]["model"] == "gpt-4o"  # siblings untouched


@pytest.mark.unit
def test_set_list_element(state):
    out = apply_patch(state, {"set": {"items[1].score": 99}})
    assert out["items"][1]["score"] == 99
    assert out["items"][0]["score"] == 1


@pytest.mark.unit
def test_set_negative_index(state):
    out = apply_patch(state, {"set": {"items[-1].name": "last"}})
    assert out["items"][-1]["name"] == "last"


@pytest.mark.unit
def test_set_new_key_requires_create_missing(state):
    with pytest.raises(PatchError, match="does not exist"):
        apply_patch(state, {"set": {"brand_new": 1}})
    out = apply_patch(state, {"set": {"brand_new": 1}}, create_missing=True)
    assert out["brand_new"] == 1


@pytest.mark.unit
def test_set_creates_intermediate_dicts_only_with_flag(state):
    patch = {"set": {"meta.fresh.leaf": "v"}}
    with pytest.raises(PatchError, match="not found"):
        apply_patch(state, patch)
    out = apply_patch(state, patch, create_missing=True)
    assert out["meta"]["fresh"]["leaf"] == "v"


@pytest.mark.unit
def test_typo_gets_did_you_mean_hint(state):
    with pytest.raises(PatchError, match="did you mean 'retries'"):
        apply_patch(state, {"set": {"meta.retres": 0}})


@pytest.mark.unit
def test_unknown_key_lists_available_keys(state):
    with pytest.raises(PatchError, match="available keys"):
        apply_patch(state, {"set": {"meta.zzzzzzzz": 0}})


# ── delete ────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_delete_field(state):
    out = apply_patch(state, {"delete": ["broken_field"]})
    assert "broken_field" not in out
    assert "query" in out


@pytest.mark.unit
def test_delete_accepts_bare_string(state):
    out = apply_patch(state, {"delete": "broken_field"})
    assert "broken_field" not in out


@pytest.mark.unit
def test_delete_nested_and_list_element(state):
    out = apply_patch(state, {"delete": ["meta.model", "items[0]"]})
    assert "model" not in out["meta"]
    assert len(out["items"]) == 1
    assert out["items"][0]["name"] == "b"


@pytest.mark.unit
def test_delete_missing_path_errors(state):
    with pytest.raises(PatchError, match="cannot delete"):
        apply_patch(state, {"delete": ["nope"]})


# ── merge ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_merge_preserves_untouched_keys(state):
    out = apply_patch(state, {"merge": {"meta": {"retries": 0}}})
    assert out["meta"]["retries"] == 0
    assert out["meta"]["model"] == "gpt-4o"


@pytest.mark.unit
def test_merge_is_shallow(state):
    out = apply_patch(state, {"merge": {"meta": {"nested": {"other": 1}}}})
    assert out["meta"]["nested"] == {"other": 1}  # replaced, not deep-merged


@pytest.mark.unit
def test_merge_non_object_value_rejected(state):
    with pytest.raises(PatchError, match="must be objects"):
        apply_patch(state, {"merge": {"meta": "not-a-dict"}})


@pytest.mark.unit
def test_merge_into_non_object_rejected(state):
    with pytest.raises(PatchError, match="expected an object to merge into"):
        apply_patch(state, {"merge": {"query": {"a": 1}}})


# ── ordering, conflicts, immutability ─────────────────────────────────────────


@pytest.mark.unit
def test_ops_apply_in_delete_set_merge_order(state):
    # 'set' recreates a key that 'delete' removed — delete must run first.
    out = apply_patch(
        state,
        {"set": {"broken_field": "rebuilt"}, "delete": ["count"]},
    )
    assert out["broken_field"] == "rebuilt"
    assert "count" not in out


@pytest.mark.unit
def test_result_independent_of_key_order(state):
    a = apply_patch(state, {"delete": ["count"], "set": {"query": "x"}})
    b = apply_patch(state, {"set": {"query": "x"}, "delete": ["count"]})
    assert a == b


@pytest.mark.unit
def test_conflicting_paths_rejected(state):
    with pytest.raises(PatchError, match="conflicting patch ops"):
        apply_patch(state, {"set": {"query": "a"}, "delete": ["query"]})


@pytest.mark.unit
def test_conflict_detection_normalizes_index_syntax(state):
    with pytest.raises(PatchError, match="conflicting patch ops"):
        apply_patch(
            state, {"set": {"items[0].name": "x"}, "merge": {"items.[0].name": {}}}
        )


@pytest.mark.unit
def test_input_state_never_mutated(state):
    before = copy.deepcopy(state)
    apply_patch(state, {"set": {"meta.retries": 0}, "delete": ["broken_field"]})
    assert state == before


@pytest.mark.unit
def test_deep_structures_are_copied_not_shared(state):
    out = apply_patch(state, {"set": {"query": "x"}})
    out["meta"]["model"] = "changed"
    assert state["meta"]["model"] == "gpt-4o"


@pytest.mark.unit
def test_empty_patch_is_a_noop(state):
    out = apply_patch(state, {})
    assert out == state
    assert preview_patch(state, {}) == []


# ── validation ────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_unknown_op_rejected(state):
    with pytest.raises(PatchError, match="unknown patch op"):
        apply_patch(state, {"replace": {"query": "x"}})


@pytest.mark.unit
def test_non_dict_patch_rejected(state):
    with pytest.raises(PatchError, match="must be a JSON object"):
        apply_patch(state, ["query", "x"])


@pytest.mark.unit
def test_non_dict_state_rejected():
    with pytest.raises(PatchError, match="state must be a dict"):
        apply_patch(["not", "a", "dict"], {"set": {"a": 1}})


@pytest.mark.unit
def test_delete_body_must_be_list_or_string(state):
    with pytest.raises(PatchError, match="must be a list of paths"):
        apply_patch(state, {"delete": {"query": True}})


@pytest.mark.unit
def test_validate_patch_does_not_need_state():
    validate_patch({"set": {"a.b": 1}, "delete": ["c"]})
    with pytest.raises(PatchError):
        validate_patch({"set": {"a..b": 1}})


@pytest.mark.unit
def test_index_out_of_range_reports_length(state):
    with pytest.raises(PatchError, match=r"list has 2 item\(s\)"):
        apply_patch(state, {"set": {"items[9].name": "x"}})


@pytest.mark.unit
def test_index_into_non_list_rejected(state):
    with pytest.raises(PatchError, match="requires a list"):
        apply_patch(state, {"set": {"meta[0]": "x"}})


# ── preview ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_preview_reports_changes_without_applying(state):
    changes = preview_patch(
        state, {"set": {"meta.retries": 0}, "delete": ["broken_field"]}
    )
    ops = {c.op: c for c in changes}
    assert ops["set"].before == 3
    assert ops["set"].after == 0
    assert ops["set"].existed is True
    assert ops["delete"].before == "PLACEHOLDER"
    assert state["meta"]["retries"] == 3  # untouched


@pytest.mark.unit
def test_preview_surfaces_errors_as_a_dry_run(state):
    with pytest.raises(PatchError):
        preview_patch(state, {"set": {"meta.nonexistent": 1}})


@pytest.mark.unit
def test_format_changes_renders_all_three_shapes(state):
    changes = preview_patch(
        state,
        {
            "set": {"meta.retries": 0, "added": "new"},
            "delete": ["broken_field"],
        },
        create_missing=True,
    )
    lines = format_changes(changes)
    assert any(line.startswith("~ meta.retries") for line in lines)
    assert any(line.startswith("+ added") for line in lines)
    assert any(line.startswith("- broken_field") for line in lines)


@pytest.mark.unit
def test_format_changes_truncates_long_values():
    state = {"blob": "x" * 500}
    changes = preview_patch(state, {"set": {"blob": "y" * 500}})
    line = format_changes(changes, max_value_chars=40)[0]
    assert "…" in line
    assert len(line) < 200


@pytest.mark.unit
def test_patch_handles_argus_signal_field_path_form():
    """A dotted_path from a SemanticSignal should paste straight into a patch."""
    state = {"result": {"items": [{"summary": "TODO: fill this in"}]}}
    signal_path = "result.items.[0].summary"
    out = apply_patch(state, {"set": {signal_path: "real summary"}})
    assert out["result"]["items"][0]["summary"] == "real summary"
