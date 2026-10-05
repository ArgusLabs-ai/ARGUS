"""build_ledger as a single unit: synthetic NodeEvents in, notebook out.

No graph, no recorder, no storage — just the fold. These pin the edges that
blame depends on: `{}` vs `None`, carry-over across a crash, per-row snapshot
independence, retries, and what the shallow copy does and does not protect.
"""

from __future__ import annotations

import pytest

from argus.ledger import build_ledger
from argus.models import NodeEvent

pytestmark = pytest.mark.unit


def _ev(i, node, update, *, inp=None, exc=None, tools=None, status="pass"):
    e = NodeEvent(
        step_index=i,
        node_name=node,
        status=status,
        input_state=inp if inp is not None else {},
        output_dict=update,
        duration_ms=1.0,
        timestamp_utc="2026-01-01T00:00:00Z",
        exception=exc,
    )
    if tools is not None:
        e.tool_calls = tools
    return e


# ── the empty / degenerate ends ─────────────────────────────────────────────


def test_no_steps_is_an_empty_notebook():
    assert build_ledger([], {"q": "x"}) == []


def test_no_initial_state_starts_empty():
    rows = build_ledger([_ev(0, "a", {"x": 1})])
    assert rows[0].state_after == {"x": 1}


def test_caller_initial_state_is_not_mutated():
    seed = {"q": "x"}
    build_ledger([_ev(0, "a", {"x": 1})], seed)
    assert seed == {"q": "x"}


# ── the signal: `{}` vs `None` ──────────────────────────────────────────────


def test_empty_update_is_kept_distinct_from_a_crash():
    rows = build_ledger([_ev(0, "silent", {}), _ev(1, "boom", None, exc="KeyError")])
    assert rows[0].update == {}  # the node really returned nothing
    assert rows[1].update is None  # no update we can read
    assert rows[1].error == "KeyError"


def test_state_carries_across_an_empty_update_and_a_crash():
    rows = build_ledger(
        [_ev(0, "search", {"docs": ["d"]}), _ev(1, "silent", {}), _ev(2, "boom", None)],
        {"q": "x"},
    )
    assert rows[1].state_after == {"q": "x", "docs": ["d"]}
    assert rows[2].state_after == {"q": "x", "docs": ["d"]}


def test_a_field_written_as_none_lands_as_none_not_absent():
    """contextual._lacks treats None as missing — the fold must not drop it."""
    rows = build_ledger([_ev(0, "a", {"summary": "s"}), _ev(1, "b", {"summary": None})])
    assert "summary" in rows[1].state_after
    assert rows[1].state_after["summary"] is None


def test_last_write_wins():
    rows = build_ledger([_ev(0, "a", {"x": 1}), _ev(1, "b", {"x": 2})])
    assert rows[1].state_after["x"] == 2


# ── per-row snapshots ───────────────────────────────────────────────────────


def test_rows_do_not_share_their_state_dict():
    rows = build_ledger([_ev(0, "a", {"x": 1}), _ev(1, "b", {"y": 2})])
    rows[0].state_after["x"] = "clobbered"
    assert rows[1].state_after["x"] == 1


def test_state_after_is_a_shallow_copy_so_nested_values_stay_shared():
    """Known ceiling: mutate a list inside the state and every row sees it."""
    docs: list = []
    rows = build_ledger([_ev(0, "a", {"docs": docs}), _ev(1, "b", {})])
    docs.append("late")
    assert rows[0].state_after["docs"] == ["late"]  # not a deep copy


def test_update_is_copied_off_the_event():
    """Reading the notebook must not be able to edit the trace behind it."""
    ev = _ev(0, "a", {"x": 1})
    row = build_ledger([ev])[0]
    ev.output_dict["x"] = 2
    assert row.update == {"x": 1}


def test_an_empty_update_survives_the_copy_as_empty_not_none():
    assert build_ledger([_ev(0, "a", {})])[0].update == {}


# ── retries, order, tools ───────────────────────────────────────────────────


def test_a_skipped_step_is_not_a_row():
    """The unchosen branch of a conditional never ran — it is not a step."""
    rows = build_ledger(
        [_ev(0, "a", {"x": 1}), _ev(-1, "never", None, status="skipped")], {"q": "s"}
    )
    assert [r.node for r in rows] == ["a"]


def test_skipping_a_step_does_not_disturb_the_running_state():
    rows = build_ledger(
        [_ev(0, "a", {"x": 1}), _ev(-1, "never", None, status="skipped"), _ev(1, "b", {"y": 2})]
    )
    assert rows[-1].state_after == {"x": 1, "y": 2}


def test_status_is_carried_on_every_row():
    rows = build_ledger([_ev(0, "a", None, status="crashed"), _ev(1, "b", {"x": 1})])
    assert [r.status for r in rows] == ["crashed", "pass"]


def test_a_retried_node_keeps_both_attempts_in_order():
    rows = build_ledger(
        [_ev(0, "flaky", None, exc="Timeout", status="retried"), _ev(1, "flaky", {"x": 1})]
    )
    assert [r.node for r in rows] == ["flaky", "flaky"]
    assert [r.status for r in rows] == ["retried", "pass"]
    assert rows[0].update is None and rows[1].update == {"x": 1}


def test_the_fold_follows_list_order_not_step_index():
    """Rows carry the event's index, but the running state follows the list."""
    rows = build_ledger([_ev(5, "late", {"x": "late"}), _ev(1, "early", {"x": "early"})])
    assert [r.step_index for r in rows] == [5, 1]
    assert rows[-1].state_after["x"] == "early"


def test_tools_default_to_empty_and_are_copied_off_the_event():
    ev = _ev(0, "a", {"x": 1}, tools=[{"name": "t", "output": 1, "error": None}])
    plain = _ev(1, "b", {})
    rows = build_ledger([ev, plain])
    assert rows[1].tools == []
    ev.tool_calls.append({"name": "t2"})
    assert len(rows[0].tools) == 1  # the list itself is a copy


def test_tool_records_are_carried_through_verbatim():
    rec = {"name": "fetch", "input": "q", "output": None, "error": "RuntimeError()"}
    rows = build_ledger([_ev(0, "a", {}, tools=[rec])])
    assert rows[0].tools[0] == rec


def test_input_state_is_carried_not_reconstructed():
    """The row's input is what the trace said went in, even if it disagrees."""
    rows = build_ledger([_ev(0, "a", {"x": 1}, inp={"seed": True})], {"other": 1})
    assert rows[0].input_state == {"seed": True}


# ── documented ceiling: reducers ────────────────────────────────────────────


def test_reduced_field_overlay_is_last_write_wins_by_design():
    """`Annotated[list, operator.add]` really accumulates; the notebook does not."""
    rows = build_ledger([_ev(0, "a", {"docs": ["x"]}), _ev(1, "b", {"docs": []})])
    assert rows[1].state_after["docs"] == []  # LangGraph would hold ["x"]
