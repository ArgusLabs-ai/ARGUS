"""Contextual logic — who wrote a field, who reads it *later*, who dropped it.

Layer 3 of the pivot (``docs/ARGUS-PIVOT-CONTRIBUTORS.pdf`` §6).

A graph shares one state object, so a field written early can still be required
late. Step A writes ``b``; B and C run in between and never had a duty to
produce it; D still needs it. Matching step N's output against step N+1's input
blames C — that is the bug this layer exists to fix. Blaming D's crash is the
bug ARGUS already had.

A fat trace records what each step returned. It never records what a later step
expected, and deriving readers from what was written would just be adjacent
matching under a new name. So the readers are **declared**::

    ArgusRecorder(consumers={"b": ["D"]}).attach(app)

Blame walks the notebook forward to the first reader and stops at the first row
whose running state lacks the field. One rule, both cases:

* nobody ever wrote ``b`` → the very first row already lacks it → origin is A,
  not C (who had no duty) and not D (who merely crashed on the consequence).
* A wrote ``b`` and C dropped it → A and B carry it, C's row does not → origin
  is C, the node that actually lost it.
"""

from __future__ import annotations

from typing import Any

from argus.findings import _mk
from argus.inspector import _is_empty
from argus.models import Finding

__all__ = ["contextual_findings"]

ConsumerMap = dict[str, list[str]]


def contextual_findings(ledger: list[Any], consumers: ConsumerMap | None) -> list[Finding]:
    """Fields a declared reader needs that were never written, or were dropped.

    Returns one critical finding per missing field, blaming the origin row.
    ``node`` is the origin; ``field_path`` is the field; the reader is named in
    the reason. Callers attach these to the origin step — the run-status roll-up
    and ``argus check`` already fail on a step with missing fields.
    """
    if not consumers or not ledger:
        return []

    out: list[Finding] = []
    for field, readers in consumers.items():
        reader_at = _first_reader_index(ledger, readers)
        if reader_at is None:
            continue  # no declared reader actually ran — nothing to require

        origin = next(
            (row for row in ledger[:reader_at] if _lacks(row.state_after, field)),
            None,
        )
        if origin is None:
            continue  # the field was there the whole way

        reader = ledger[reader_at].node
        out.append(
            _mk(
                node=origin.node,
                type_="missing_field",
                severity="critical",
                reason=(
                    f"Field `{field}` is read later by `{reader}` but was not present "
                    f"after `{origin.node}` ran."
                ),
                source="heuristic",
                field_path=field,
                origin_node=origin.node,
            )
        )
    return out


def _first_reader_index(ledger: list[Any], readers: list[str]) -> int | None:
    names = set(readers)
    return next((i for i, row in enumerate(ledger) if row.node in names), None)


def _lacks(state: dict[str, Any], field: str) -> bool:
    """Absent, None, blank, or an empty collection.

    Reuses the inspector's own rule so a field dropped to `[]` or `""` reads
    the same here as it does everywhere else in ARGUS. Narrower than that —
    None-only — silently misses the commonest drop: a filter step that removes
    every element and returns `{"docs": []}`.
    """
    return _is_empty(state.get(field))
