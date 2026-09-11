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

Blame is anchored at the **reader**, not at the start of the run. A field that
does not exist yet is not a failure — most pipelines fill state progressively
(``ingest`` writes ``query``, ``retrieve`` writes ``sources``, ``draft`` reads
them). Asking "was it there when the reader started?" is the whole first test;
walking forward from step 0 and stopping at the first row that lacks the field
blames ``ingest`` for not having done ``retrieve``'s job.

Only when the field really is missing at the reader do we walk the field's
history backward:

* **written then dropped** — it was present, then a later row lost it →
  origin is the row that lost it (not the reader, not the reader's neighbour).
* **written empty** — a step wrote the key with ``[]`` / ``""`` / ``None`` →
  origin is that writer (``retrieve`` returning ``{"sources": []}``).
* **never written** — the key appears in no update before the reader → origin
  is the first row, since nothing in a trace says who was supposed to produce
  it. A producer map would; we do not have one and do not guess.

A node that returned a literal ``{}`` is not this layer's business —
``inspector.empty_output`` already blames it. When such a row sits between the
start and the reader, stay quiet rather than add a second, worse-aimed finding.
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
        finding = _blame(ledger, field, reader_at)
        if finding is not None:
            out.append(finding)
    return out


def _blame(ledger: list[Any], field: str, reader_at: int) -> Finding | None:
    """Who is answerable for `field` being missing when its reader ran."""
    before = ledger[:reader_at]
    reader = ledger[reader_at]
    if not before:
        return None  # the reader ran first — nobody upstream to blame

    # The reader produces the field it consumes (accumulator, initialiser). It
    # is not missing — the reader is where it comes from.
    if _wrote(reader, field):
        return None

    # Anchor on the reader. Not yet written is not a failure.
    if not _lacks(reader.input_state, field):
        return None

    # Index the field's history over the rows that ran before the reader.
    held = [i for i, row in enumerate(before) if not _lacks(row.state_after, field)]
    wrote = [i for i, row in enumerate(before) if _wrote(row, field)]

    if held:
        # Present, then lost: blame the row that lost it.
        origin_at = next(
            (i for i in range(held[-1] + 1, len(before)) if _lacks(before[i].state_after, field)),
            None,
        )
        if origin_at is None:
            # The notebook says it survived every row yet the reader did not get
            # it — the overlay and the real reducer merge disagree. Say nothing
            # rather than blame a row on bad evidence.
            return None
        why = f"`{before[origin_at].node}` dropped it"
    elif wrote:
        # Written, but written empty.
        origin_at = wrote[0]
        why = f"`{before[origin_at].node}` wrote it empty"
    else:
        # Never written by anyone. A node that returned `{}` is already blamed
        # by inspector.empty_output — defer to it instead of adding a second
        # finding aimed at whoever happened to run first.
        if any(row.update == {} for row in before):
            return None
        origin_at = 0
        why = "no step wrote it"

    origin = before[origin_at]
    return _mk(
        node=origin.node,
        type_="missing_field",
        severity="critical",
        reason=f"Field `{field}` is read by `{reader.node}` but {why}.",
        source="heuristic",
        field_path=field,
        origin_node=origin.node,
    )


def _first_reader_index(ledger: list[Any], readers: list[str]) -> int | None:
    names = set(readers)
    return next((i for i, row in enumerate(ledger) if row.node in names), None)


def _wrote(row: Any, field: str) -> bool:
    return isinstance(row.update, dict) and field in row.update


def _lacks(state: dict[str, Any], field: str) -> bool:
    """Absent, None, blank, or an empty collection.

    Reuses the inspector's own rule so a field dropped to `[]` or `""` reads
    the same here as it does everywhere else in ARGUS. Narrower than that —
    None-only — silently misses the commonest drop: a filter step that removes
    every element and returns `{"docs": []}`.
    """
    return _is_empty(state.get(field))
