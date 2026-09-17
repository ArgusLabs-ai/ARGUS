"""The ledger — the fat trace folded into a notebook.

One row per step: what went in, what the step *returned* (the update, not the
merged state pile), the running state after that step, its tool I/O, and its
error. Origin blame needs the update: a node that searches, throws the result
away and returns ``{}`` still leaves a full-looking merged state behind, so
"state after" alone hides the silent no-op.

Layer 2 of the pivot (``docs/ARGUS-PIVOT-CONTRIBUTORS.pdf`` §5). The notebook is
*derived* from ``RunRecord.steps``, not stored beside them: no second database,
no second file. Every column survives the existing run file, so re-score is just
``build_ledger(load_run(id).steps, ...)`` — open the file and grade it, no live
graph and no second invoke.

Rows are folded in the order the steps were recorded — execution order, not
``step_index`` order. For a fan-out that *is* the truth: two branches have no
meaningful index ordering between them, only the order they finished in.

A row is not enough to replay its step. Values too large for
``max_field_size``, and anything that would not serialize, are already markers
(``{"__argus_truncated__": True, ...}``) by the time the notebook sees them —
faithful for grading, since a marker reads as the present, non-empty value it
stands for, but not the original payload.

What is not here yet: HTTP I/O (tool callbacks are the I/O we can see today) and
replaying a step by re-executing the node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from argus.inspector import _is_empty

__all__ = ["LedgerRow", "build_ledger", "reducer_kinds"]

# Reducer callables do not survive the run file, so the notebook folds by *kind*
# — a string that does. `__name__` of the declared reducer, mapped to how the
# fold combines it. `add_messages` is treated as concatenation: it really
# de-duplicates by message id, so an updated message counts twice here.
_ADD_REDUCERS = frozenset({"add", "iadd", "concat", "add_messages"})


def reducer_kinds(reducer_fields: dict[str, Any] | None) -> dict[str, str]:
    """``{field: reducer_callable}`` → ``{field: kind}``, one string per field.

    Anything unrecognised — a custom lambda, a domain-specific merge — is
    ``"overwrite"``, which is what the fold did for every field before. Guessing
    at an unknown callable's semantics off its name would put a state in the
    notebook that never existed.
    """
    return {
        name: ("add" if _reducer_name(fn) in _ADD_REDUCERS else "overwrite")
        for name, fn in (reducer_fields or {}).items()
    }


def _reducer_name(fn: Any) -> str:
    """The reducer's name, without the private-alias underscore.

    LangGraph exports `add_messages` but the callable on the annotation is the
    undecorated `_add_messages`. Matching the raw `__name__` therefore folded
    `MessagesState` — the state most graphs use — as overwrite, so every row's
    running state held only the last node's messages.
    """
    return getattr(fn, "__name__", "").lstrip("_")


@dataclass(frozen=True)
class LedgerRow:
    """One step of the run, as recorded."""

    step_index: int
    node: str
    input_state: dict[str, Any]
    update: dict[str, Any] | None
    state_after: dict[str, Any]
    tools: list[dict[str, Any]] = field(default_factory=list)
    # Where the node routed itself with a `Command` handoff; empty otherwise (#110).
    goto: list[str] = field(default_factory=list)
    error: str | None = None
    # The event's own status (docs/STATUS.md). Carried so a consumer can tell a
    # retried attempt from a clean one; without it every non-update row looks
    # the same and a row that never ran reads as one that ran and produced
    # nothing.
    status: str = "pass"


def _fold(
    running: dict[str, Any], update: dict[str, Any], kinds: dict[str, str]
) -> dict[str, Any]:
    """Merge one update into the running state, honouring reduced fields."""
    merged = dict(running)
    for key, value in update.items():
        if kinds.get(key) == "add" and key in merged:
            try:
                merged[key] = merged[key] + value
            except TypeError:
                merged[key] = value  # e.g. the field was None before
        else:
            merged[key] = value
    return merged


def _believe_the_trace(
    running: dict[str, Any], ran: list[Any], position: int
) -> dict[str, Any]:
    """Undo a fold that says "empty" where the trace says otherwise (#80).

    The fold emulates reducers from a *name* (``reducer_kinds``), because
    callables do not survive the run file. Anything it does not recognise —
    a custom merge, a last-good-wins keeper, a domain-specific combine — folds
    as overwrite, so a step returning ``{"docs": []}`` leaves the notebook
    claiming ``docs`` is empty when the real reducer kept the previous value.
    That is the notebook contradicting the same run file it was built from:
    the *next* step's recorded ``input_state`` is the merged state LangGraph
    actually handed it, reducers already applied.

    So where the two disagree, the recording wins. Deliberately one-directional:
    a correction only ever restores a value the next step really received, and
    can never invent an emptiness the trace does not show. That keeps the one
    failure this could otherwise cause — blaming a node for dropping a field
    the reducer preserved — without letting the repair hide a genuine drop.

    Resolving reducers by import path was the other candidate, and it is the
    one #79 just ruled out: grading must not import the user's code.

    Ceiling: under fan-out the next step to run may be a sibling that never saw
    this step's update, so a branch that really did empty a field can read as
    still holding the pre-fan-out value. Blame anchors on the reader's own
    recorded input (:mod:`argus.contextual`), which this never edits, so that
    costs a row's display rather than a verdict.
    """
    nxt = ran[position + 1] if position + 1 < len(ran) else None
    if nxt is None:
        return running
    recorded = getattr(nxt, "input_state", None)
    if not isinstance(recorded, dict):
        return running

    corrected: dict[str, Any] | None = None
    for key, value in recorded.items():
        if key in running and _is_empty(running[key]) and not _is_empty(value):
            corrected = running if corrected is not None else dict(running)
            corrected[key] = value
    return corrected if corrected is not None else running


def build_ledger(
    steps: list[Any],
    initial_state: dict[str, Any] | None = None,
    reducers: dict[str, str] | None = None,
    state_keys: list[str] | None = None,
) -> list[LedgerRow]:
    """Fold recorded steps into the notebook.

    ``steps`` are ``NodeEvent``s — live from the recorder, or reloaded from
    ``.argus/runs/<id>.json``. Both give the same notebook, which is what makes
    re-scoring an old run possible with no live graph.

    Retried attempts are kept — the attempt ran, and later rows are the evidence
    that a downstream node was the victim rather than the origin. Steps marked
    ``skipped`` are not: those are the unchosen branch of a conditional edge,
    synthesized at finalize (``session._finalize``) for a node that never
    executed. It has no input, no update and no running state, so it is not a
    step in the notebook, and leaving it in let a node that never ran be treated
    as a reader by :mod:`argus.contextual`. Mirrors the run roll-up, which has
    always excluded both.

    Dropping them is also what keeps this fold honest across a reload: skipped
    events exist only after finalize, so a live ledger and one rebuilt from the
    run file would otherwise disagree on any graph with a conditional edge.
    """
    # ponytail: dict overlay, last write wins, except for fields `reducers` says
    # accumulate. Kinds are strings on purpose — a callable would not survive the
    # run file, and a live-only fix would make the reloaded notebook disagree
    # with the live one.
    #
    # A custom reducer still round-trips as "overwrite", because its name tells
    # us nothing. Rather than emulate it, `_believe_the_trace` repairs the fold
    # from the next step's recorded `input_state` where the two disagree in the
    # one direction that causes false blame (#80). Remaining ceiling: a fan-in
    # folded by a custom reducer still reads as the last branch winning, since
    # no single successor's input can show what the merge did.
    kinds = reducers or {}
    # A subgraph node writes into the subgraph's schema. A key that exists only
    # there is invisible to every node outside it, so carrying it in the running
    # state tells a later reader a field was waiting for it that never was — and
    # `contextual` then clears the node that should have been blamed. The
    # node's own recorded `input_state` is untouched: this scopes the notebook,
    # not the trace. Unknown (empty) keeps the old fold.
    outer = set(state_keys or ())
    running: dict[str, Any] = dict(initial_state or {})
    rows: list[LedgerRow] = []

    ran = [e for e in steps if e.status != "skipped"]
    for position, event in enumerate(ran):
        update = event.output_dict
        if update:
            # Fold the outward-visible part; the row still reports the update
            # the node actually returned.
            visible = {k: v for k, v in update.items() if k in outer} if outer else update
            running = _fold(running, visible, kinds)
        running = _believe_the_trace(running, ran, position)
        rows.append(
            LedgerRow(
                step_index=event.step_index,
                node=event.node_name,
                input_state=event.input_state,
                # Copied, so a consumer reading the notebook cannot edit the
                # trace it was built from. Shallow: rows deliberately share
                # nested values rather than deep-copying a state pile per step.
                update=dict(update) if update is not None else None,
                state_after=dict(running),
                tools=list(getattr(event, "tool_calls", ()) or ()),
                goto=list(getattr(event, "goto", ()) or ()),
                error=event.exception,
                status=event.status,
            )
        )

    return rows
