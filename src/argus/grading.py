"""The grading chain, with no graph engine attached.

A LangGraph callback (:class:`argus.recorder.ArgusRecorder`) and a trace file
both end up with the same thing: node names, edges, and one row per step. From
there grading is identical — refuse a trace that cannot be trusted, then ledger
→ contextual (:mod:`argus.contextual`) → the session's structure / tool /
semantic checks → judge last → verdict. This module is that chain, so a caller
without a live app can use it. It imports nothing from ``langgraph`` or
``langchain_core``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Any

from argus.contextual import ConsumerMap, contextual_findings
from argus.ledger import build_ledger
from argus.models import Finding, LLMInvestigationConfig
from argus.session import ArgusSession

__all__ = ["IncompleteTraceError", "finish", "new_session"]


class IncompleteTraceError(RuntimeError):
    """The recording was too thin to grade.

    Brief §4: an incomplete recording is never a pass. Refusing loudly beats
    reporting "no findings, so it passed" off a trace that never arrived.
    """


def _placeholder_node(name: str) -> Callable[..., Any]:
    """A stand-in for a node function the trace never gives us.

    ``ArgusSession._get_successor_fns`` looks successors up in
    ``node_fn_registry``; an empty registry means no successors, and the
    critical ``empty_output`` rule (``inspector.py``) only fires when a node has
    successors waiting. The placeholder restores that. It is deliberately
    unannotated: a trace does not carry what the next step expected, so the
    structural field check is skipped rather than quietly inventing a contract.
    That contract is layer 3 (:mod:`argus.contextual`), declared with
    ``consumers=``.

    The marker tells the inspector this successor is ours, not the user's, so it
    does not advise "add type hints to X" — advice that is wrong on an annotated
    node and impossible to act on either way.
    """

    def _node(state):  # type: ignore[no-untyped-def]  # unannotated on purpose
        raise RuntimeError(f"{name} is a trace placeholder and is never called")

    _node.__name__ = name
    _node.__argus_trace_placeholder__ = True  # type: ignore[attr-defined]
    return _node


def new_session(
    node_names: list[str],
    edge_map: dict[str, list[str]],
    conditional_sources: set[str],
    reducer_fields: dict[str, Any],
    *,
    judge: bool,
    validators: dict[str, Callable[[dict[str, Any]], tuple[bool, str]]],
    strict: bool,
    max_field_size: int,
    state_keys: list[str] | None = None,
) -> ArgusSession:
    """A session for one graph run, ready for ``on_node_start`` / ``on_node_end``.

    ``state_keys`` are the keys the graph's own state has. Passing them keeps a
    subgraph's inner-only field out of the ledger's running state, where it
    would otherwise look available to a node that can never read it
    (:class:`argus.models.RunRecord`). A caller with no schema omits it.
    """
    session = ArgusSession(
        max_field_size=max_field_size,
        validators=validators,
        strict=strict,
        # Explicit config (never None) so the session does not fall back to
        # its own auto-enable logic — the caller owns the decision here.
        llm_investigation=LLMInvestigationConfig(
            enabled=judge,
            always_investigate=judge,
            semantic_check=judge,
        ),
    )
    session.set_node_names(node_names)
    session.set_edges(edge_map)
    session.set_conditional_sources(conditional_sources)
    session.node_fn_registry = {name: _placeholder_node(name) for name in node_names}
    session.reducer_fields = reducer_fields
    session.state_keys = list(state_keys or ())
    # The caller owns finalize: the ledger and contextual layers run over the
    # complete trace, before the run is graded and saved.
    session._defer_auto_finalize = True
    return session


def finish(
    session: ArgusSession,
    consumers: ConsumerMap | None,
    unfinished: Iterable[str] = (),
) -> None:
    """Ledger → contextual → structure/tools + semantic → judge → verdict.

    ``unfinished`` names steps that started but never reported an update; any
    at all refuses the run.
    """
    unfinished = list(unfinished)
    if unfinished:
        _refuse(
            session,
            f"steps started but never reported an update: {', '.join(unfinished)}",
        )
    if not session._events:
        _refuse(session, "no steps were recorded — the trace is empty")

    ledger = build_ledger(
        session._events, session._initial_state, session.reducer_kinds, session.state_keys
    )
    _blame_origins(session, contextual_findings(ledger, consumers))

    # The per-step judge already fired (its futures don't re-check this
    # flag); disabling it here only stops finalize from also running the
    # investigate() essay — a second LLM call that is not the verdict.
    if session._llm_investigation_config is not None:
        session._llm_investigation_config.enabled = False

    # Everything after this is the existing ARGUS brain: per-step structure,
    # tool and semantic checks already ran inside on_node_end; finalize rolls
    # them up, applies the judge last, collects findings and saves the run.
    session.finalize()

    # So a bare `argus check` grades this run (cli/cmd_check.py reads it).
    os.environ["ARGUS_RUN_ID"] = session.run_id


def _blame_origins(session: ArgusSession, findings: list[Finding]) -> None:
    """Record a contextual miss on the step that caused it.

    No second gate: a step carrying `missing_fields` already fails the
    roll-up in `session._finalize` and `check.evaluate_run`, and
    `findings.collect_findings` already turns it into a `missing_field`
    finding naming the origin. Must run before finalize.
    """
    by_node = {event.node_name: event for event in session._events}
    for finding in findings:
        event = by_node.get(finding.node)
        if event is None or event.inspection is None or finding.field_path is None:
            continue
        insp = event.inspection
        if finding.field_path not in insp.missing_fields:
            insp.missing_fields.append(finding.field_path)
        insp.is_silent_failure = True
        insp.severity = "critical"
        # Accumulate: two consumers can miss two fields on one origin, and a
        # structural or tool message may already be here. Overwriting would
        # keep only the last reason and drop what the earlier layers authored.
        if insp.message == "All checks passed":
            insp.message = finding.reason
        elif finding.reason not in insp.message:
            insp.message = f"{insp.message}; {finding.reason}"
        event.status = "fail"


def _refuse(session: ArgusSession, why: str) -> None:
    """Abandon the run rather than grade a recording we cannot trust.

    The session's atexit safety net would otherwise finalize this run on
    interpreter exit and, finding no failures in a trace that never arrived,
    save it as clean — the exact "no findings, so it passed" the brief bans.
    Marking it complete makes that finalize a no-op.
    """
    session._completed = True
    raise IncompleteTraceError(f"{why}. An incomplete recording is not a pass.")
