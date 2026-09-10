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

What is not here yet: HTTP I/O (tool callbacks are the I/O we can see today) and
replaying a step by re-executing the node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["LedgerRow", "build_ledger"]


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


def build_ledger(
    steps: list[Any],
    initial_state: dict[str, Any] | None = None,
) -> list[LedgerRow]:
    """Fold recorded steps into the notebook.

    ``steps`` are ``NodeEvent``s — live from the recorder, or reloaded from
    ``.argus/runs/<id>.json``. Both give the same notebook, which is what makes
    re-scoring an old run possible with no live graph.

    Retried and skipped events are kept: later rows are the evidence that a
    downstream node was the victim rather than the origin.
    """
    # ponytail: plain dict overlay for the running state. Reducer-aware merging
    # already lives in session.py; wire it in when the ledger drives replay.
    running: dict[str, Any] = dict(initial_state or {})
    rows: list[LedgerRow] = []

    for event in steps:
        update = event.output_dict
        if update:
            running = {**running, **update}
        rows.append(
            LedgerRow(
                step_index=event.step_index,
                node=event.node_name,
                input_state=event.input_state,
                update=update,
                state_after=dict(running),
                tools=list(getattr(event, "tool_calls", ()) or ()),
                error=event.exception,
            )
        )

    return rows
