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
        name: ("add" if getattr(fn, "__name__", "") in _ADD_REDUCERS else "overwrite")
        for name, fn in (reducer_fields or {}).items()
    }


@dataclass(frozen=True)
class LedgerRow:
    """One step of the run, as recorded."""

    step_index: int
    node: str
    input_state: dict[str, Any]
    update: dict[str, Any] | None
    state_after: dict[str, Any]
    tools: list[dict[str, Any]] = field(default_factory=list)
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


def build_ledger(
    steps: list[Any],
    initial_state: dict[str, Any] | None = None,
    reducers: dict[str, str] | None = None,
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
    # with the live one. Remaining ceiling: a custom reducer round-trips as
    # "overwrite", so its fan-in still reads as the last branch winning. Widen
    # `_ADD_REDUCERS`, or persist something richer than a name, if that bites.
    kinds = reducers or {}
    running: dict[str, Any] = dict(initial_state or {})
    rows: list[LedgerRow] = []

    for event in steps:
        if event.status == "skipped":
            continue
        update = event.output_dict
        if update:
            running = _fold(running, update, kinds)
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
                error=event.exception,
                status=event.status,
            )
        )

    return rows
