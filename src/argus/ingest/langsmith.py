"""``argus ingest langsmith`` — grade a LangSmith JSONL export with no app.

A LangSmith export of a LangGraph run carries what the recorder hears live: one
chain run per node step, named by ``extra.metadata.langgraph_node``, with the
state going in and the update coming out. This module turns those rows into
``on_node_end`` calls and hands the session to :func:`argus.grading.finish`.
It imports nothing from ``langgraph`` or ``langchain_core``.

A silent node arrives with ``outputs`` absent or ``{}`` (the tracer drops an
empty update on its end-PATCH — ``docs/prd_abhishek.md`` BUG-1), so both read
as the ``{}`` update.

Tool runs beneath a node step become that step's ``tool_calls``, in the dict
shape the live recorder builds, so a tool's swallowed failure is graded.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from argus.contextual import ConsumerMap
from argus.grading import finish, new_session
from argus.session import ArgusSession

__all__ = [
    "CloudSyncRefused",
    "TracedError",
    "ingest_langsmith",
    "load_runs",
    "node_runs",
    "tool_calls_by_step",
]

# Marks a graph step; LangGraph's inner runnables carry `seq:step:N` instead.
_GRAPH_STEP = re.compile(r"^graph:step:\d+$")


class CloudSyncRefused(RuntimeError):
    """Saving would upload the ingested trace, and nobody said that was fine."""


class TracedError(Exception):
    """A node's error as the trace recorded it; ``str()`` is that text verbatim."""


def load_runs(path: Path) -> list[dict[str, Any]]:
    """Read JSONL rows, merging rows that share an ``id`` (later non-null wins)."""
    runs: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            merged = runs.setdefault(str(row["id"]), {})
            merged.update({key: value for key, value in row.items() if value is not None})
    return list(runs.values())


def _node_name(run: dict[str, Any]) -> str | None:
    return ((run.get("extra") or {}).get("metadata") or {}).get("langgraph_node")


def _step(run: dict[str, Any]) -> int:
    return int(((run.get("extra") or {}).get("metadata") or {}).get("langgraph_step") or 0)


def node_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The graph's node steps, in execution order, subgraph parents dropped.

    A node run with another node run beneath it is a subgraph's parent: its
    outputs are the subgraph's merged state, so recording it would double-count
    every inner step (upstream's recorder skips the same row).
    """
    selected = {
        str(run["id"]): run
        for run in runs
        if run.get("run_type") == "chain"
        and _node_name(run)
        and any(_GRAPH_STEP.match(tag) for tag in run.get("tags") or [])
    }
    by_id = {str(run["id"]): run for run in runs}
    parents: set[str] = set()
    for run in selected.values():
        parent = run.get("parent_run_id")
        while parent is not None and str(parent) in by_id:
            if str(parent) in selected:
                parents.add(str(parent))
            parent = by_id[str(parent)].get("parent_run_id")
    kept = [run for run_id, run in selected.items() if run_id not in parents]
    return sorted(kept, key=lambda run: (_step(run), run.get("dotted_order") or ""))


def _duration_ms(run: dict[str, Any]) -> float:
    try:
        started = datetime.fromisoformat(str(run["start_time"]))
        ended = datetime.fromisoformat(str(run["end_time"]))
    except (KeyError, ValueError):
        return 0.0
    return max((ended - started).total_seconds() * 1000, 0.0)


def tool_calls_by_step(
    runs: list[dict[str, Any]], steps: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Step run id → the tool runs beneath it, in the recorder's dict shape.

    A tool belongs to its nearest step ancestor. LangSmith stores a tool's
    result as ``outputs["output"]``; the recorder hears the bare value, so it is
    unwrapped here.
    """
    by_id = {str(run["id"]): run for run in runs}
    step_ids = {str(run["id"]) for run in steps}
    tools: dict[str, list[dict[str, Any]]] = {}
    ordered = sorted(runs, key=lambda run: run.get("dotted_order") or "")
    for run in ordered:
        if run.get("run_type") != "tool":
            continue
        parent = run.get("parent_run_id")
        while parent is not None and str(parent) in by_id and str(parent) not in step_ids:
            parent = by_id[str(parent)].get("parent_run_id")
        if parent is None or str(parent) not in step_ids:
            continue
        outputs = run.get("outputs")
        tools.setdefault(str(parent), []).append(
            {
                "name": run.get("name") or "tool",
                "input": run.get("inputs"),
                "output": outputs.get("output") if isinstance(outputs, dict) else outputs,
                "error": run.get("error"),
            }
        )
    return tools


def _step_order_edges(steps: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Each node → the nodes at the next step seen in this trace.

    The trace has no graph, and with no successors the critical
    ``empty_output`` rule never fires. "A later step exists" is the successor
    this trace can prove; real edges arrive with ``argus edges`` (S-5).
    """
    at_step: dict[int, list[str]] = {}
    for run in steps:
        names = at_step.setdefault(_step(run), [])
        if _node_name(run) not in names:
            names.append(str(_node_name(run)))
    order = sorted(at_step)
    edges: dict[str, list[str]] = {}
    for here, after in zip(order, order[1:]):
        for name in at_step[here]:
            edges.setdefault(name, [])
            edges[name] += [n for n in at_step[after] if n not in edges[name]]
    return edges


def ingest_langsmith(
    path: Path,
    *,
    consumers: ConsumerMap | None = None,
    allow_cloud: bool = False,
    max_field_size: int = 50_000,
) -> ArgusSession:
    """Grade one exported run and save it; returns the finished session.

    Raises :class:`CloudSyncRefused` when logged in to ARGUS cloud without
    ``allow_cloud`` (saving would upload the trace), ``ValueError`` when the
    file does not hold exactly one root run, and
    :class:`argus.grading.IncompleteTraceError` when there is nothing to grade.
    The LLM judge stays off: grading a file spends nothing.
    """
    from argus.cloud import is_logged_in

    if not allow_cloud and is_logged_in():
        raise CloudSyncRefused(
            "logged in to ARGUS cloud: saving this run would upload the trace. "
            "Log out, or pass --allow-cloud"
        )

    runs = load_runs(path)
    roots = [run for run in runs if run.get("parent_run_id") is None]
    if len(roots) != 1:
        raise ValueError(f"expected one root run in {path}, found {len(roots)}")

    steps = node_runs(runs)
    names = list(dict.fromkeys(str(_node_name(run)) for run in steps))
    session = new_session(
        names,
        _step_order_edges(steps),
        set(),
        {},
        judge=False,
        validators={},
        strict=False,
        max_field_size=max_field_size,
    )
    tools = tool_calls_by_step(runs, steps)
    root_inputs = roots[0].get("inputs")
    session.capture_state(root_inputs if isinstance(root_inputs, dict) else {})

    for run in steps:
        node = str(_node_name(run))
        inputs = run.get("inputs")
        input_snap = session.capture_state(inputs if isinstance(inputs, dict) else {})
        outputs = run.get("outputs")
        error = run.get("error")
        exc = TracedError(str(error)) if error else None
        if exc is not None:
            output_snap = None
        elif isinstance(outputs, dict) and outputs:
            output_snap = session.capture_output(outputs)
        else:
            output_snap = {}
        session.on_node_start(node, input_snap)
        # Tools go in with the step: graders run inside on_node_end (#86).
        session.on_node_end(
            node,
            input_snap,
            output_snap,
            _duration_ms(run),
            exc=exc,
            tool_calls=tools.get(str(run["id"]), []),
        )

    finish(session, consumers)
    return session
