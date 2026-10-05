"""#148: a healthy run can suggest who was handed a field. It does not grade."""

from __future__ import annotations

import pytest

from argus.contextual import propose_consumers
from argus.ledger import LedgerRow

pytestmark = pytest.mark.unit


def _row(node: str, incoming: dict, update: dict | None) -> LedgerRow:
    merged = dict(incoming)
    if update:
        merged.update(update)
    return LedgerRow(
        step_index=0,
        node=node,
        input_state=incoming,
        update=update,
        state_after=merged,
    )


def test_later_nodes_that_received_a_field_are_candidates():
    ledger = [
        _row("A", {"seed": "s"}, {"b": "ok"}),
        _row("B", {"seed": "s", "b": "ok"}, {"noise_b": "x"}),
        _row("D", {"seed": "s", "b": "ok", "noise_b": "x"}, {"answer": "used ok"}),
    ]
    assert propose_consumers(ledger)["b"] == ["B", "D"]


def test_a_node_that_rewrites_the_field_is_not_a_reader():
    ledger = [
        _row("A", {}, {"b": "ok"}),
        _row("C", {"b": "ok"}, {"b": "still"}),
    ]
    assert "b" not in propose_consumers(ledger)


def test_a_dotted_leaf_is_proposed_from_a_nested_write():
    ledger = [
        _row("letter", {}, {"email": {"subject": "Re: order", "body": "paid"}}),
        _row("send_email", {"email": {"subject": "Re: order", "body": "paid"}}, {"sent": True}),
    ]
    proposed = propose_consumers(ledger)
    assert proposed["email.body"] == ["send_email"]
    assert "letter" not in proposed["email.body"]


def test_a_field_nobody_wrote_is_omitted():
    ledger = [
        _row("A", {"seed": "s"}, {"noise": "x"}),
        _row("D", {"seed": "s", "noise": "x"}, {"answer": "y"}),
    ]
    assert "seed" not in propose_consumers(ledger)


def test_an_empty_ledger_proposes_nothing():
    assert propose_consumers([]) == {}
