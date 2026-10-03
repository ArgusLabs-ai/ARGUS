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

from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from argus.contextual import GUESS_CONFIDENCE, ConsumerMap, contextual_findings, readers_of
from argus.ledger import build_ledger
from argus.models import Finding, InspectionResult, LLMInvestigationConfig, ToolFailure
from argus.review import (
    Review,
    Reviewer,
    confirm_warnings,
    hit_stands,
    settle_step_signals,
    split_hits,
)
from argus.session import ArgusSession
from argus.trace_rules import PRODUCED_NOTHING, Hit, run_rules

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
    consumers: ConsumerMap | None = None,
    node_state_keys: dict[str, list[str]] | None = None,
    baseline: dict[str, Any] | None = None,
    reviewer: Reviewer | None = None,
) -> ArgusSession:
    """A session for one graph run, ready for ``on_node_start`` / ``on_node_end``.

    ``state_keys`` are the keys the graph's own state has. Passing them keeps a
    subgraph's inner-only field out of the ledger's running state, where it
    would otherwise look available to a node that can never read it
    (:class:`argus.models.RunRecord`). A caller with no schema omits it.

    ``consumers`` is the same map :func:`finish` grades with. The session keeps
    it so the judge can scope the run history it is shown to the fields a node
    declares it reads (#85); blame itself still happens in :mod:`argus.contextual`.

    ``node_state_keys`` are a subgraph node's own schema keys (it writes those,
    not the parent's). ``baseline`` is a healthy-run shape from ``argus
    baseline``; without one the baseline rules in :mod:`argus.trace_rules` are off.
    ``reviewer`` settles the rules' heuristic calls (:mod:`argus.review`); without
    one the rules alone decide.
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
    session.consumers = dict(consumers or {})
    session.node_state_keys = dict(node_state_keys or {})
    session.baseline = baseline
    session.reviewer = reviewer
    # The caller owns finalize: the ledger and contextual layers run over the
    # complete trace, before the run is graded and saved.
    session._defer_auto_finalize = True
    return session


def finish(
    session: ArgusSession,
    consumers: ConsumerMap | None,
    unfinished: Iterable[str] = (),
) -> None:
    """Ledger → reviewer → rules → contextual → reviewer verdicts → verdict.

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
    merged = _merged_state_steps(session._events)
    if merged is not None:
        _refuse(
            session,
            "step outputs look like merged graph state, not node updates "
            f"(every input field carried into the output on {', '.join(merged)}) — "
            "`empty_output` and blame cannot work on that; record each node's "
            "returned dict",
        )

    ledger = build_ledger(
        session._events, session._initial_state, session.reducer_kinds, session.state_keys
    )
    # The reviewer reads only the ledger, so it runs first; everything after it
    # is deterministic given what it verified. No reviewer → rules alone decide.
    review = session.reviewer(ledger, session._events) if session.reviewer is not None else None
    if review is not None:
        settle_step_signals(session._events, review)
    keep = partial(hit_stands, review) if review is not None and review.ok else None
    # Whole-trace rules next, so the contextual layer below can defer to them.
    hits = run_rules(
        session._events,
        state_keys=session.state_keys,
        node_state_keys=session.node_state_keys,
        consumers=consumers,
        baseline=session.baseline,
        keep=keep,
    )
    stand, demoted = split_hits(hits, review)
    _apply_hits(stand)
    _note_unconfirmed(demoted)
    # A barren subgraph (S5), or a node that wrote its field under the wrong
    # name or not at all, is already failed; "never written" should not also
    # blame whichever step ran first.
    produced_nothing = frozenset(
        e.node_name
        for e in session._events
        if e.inspection is not None
        and any(
            t.failure_type == "subgraph_no_contribution" or t.failure_type in PRODUCED_NOTHING
            for t in e.inspection.tool_failures
        )
    )
    found = contextual_findings(
        ledger,
        consumers,
        blamed_elsewhere=produced_nothing,
        failed=frozenset(e.node_name for e in session._events if e.status in ("fail", "crashed")),
    )
    _blame_origins(session, _confirmed_findings(session, found, review, consumers))
    if review is not None:
        confirm_warnings(session._events, review)

    # The per-step judge already fired (its futures don't re-check this
    # flag); disabling it here only stops finalize from also running the
    # investigate() essay — a second LLM call that is not the verdict.
    if session._llm_investigation_config is not None:
        session._llm_investigation_config.enabled = False

    # Everything after this is the existing ARGUS brain: per-step structure,
    # tool and semantic checks already ran inside on_node_end; finalize rolls
    # them up, applies the judge last, collects findings and saves the run.
    session.finalize()

    # Do not write ARGUS_RUN_ID here (#90). One attach serves many runs
    # (``.batch()``, a loop, a served app); a process-global pointer would
    # silently grade whichever item finished last, and would leak into later
    # ``argus check`` / pytest / notebook sessions. Bare ``argus check`` falls
    # back to ``last`` (newest file under ``.argus/runs/``). For a specific
    # batch item, pass the id or set ``ARGUS_RUN_ID`` yourself — see
    # ``recorder.run_ids``.


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
        if event is None or finding.field_path is None:
            continue
        insp = event.inspection
        created = False
        if insp is None:
            created = True
            # A router-only step (`Command(goto=...)`, no update) is never
            # inspected. Skipping it dropped the blame and graded the run clean.
            # A crashed step keeps `crashed`; the omit is still named.
            insp = InspectionResult(
                is_silent_failure=True,
                missing_fields=[],
                empty_fields=[],
                type_mismatches=[],
                severity="critical",
                message=finding.reason,
            )
            event.inspection = insp
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
        if not (created and event.status == "crashed"):
            event.status = "fail"


def _confirmed_findings(
    session: ArgusSession,
    found: list[Finding],
    review: Review | None,
    consumers: ConsumerMap | None,
) -> list[Finding]:
    """Drop a "never written" guess the reviewer did not confirm; note it instead.

    The guess names two steps: the origin it picked and the reader that went
    without. The reviewer verifying either one corroborates it — a starved reader
    that wrote a wrong answer ("Total charged: $0.00" with no payment step) is the
    evidence, even when the reviewer did not also point at the origin.
    """
    if review is None or not review.ok:
        return found
    by_node = {event.node_name: event for event in session._events}
    keep: list[Finding] = []
    for f in found:
        if f.confidence != GUESS_CONFIDENCE or any(
            review.confirms(n) for n in {f.node} | readers_of(consumers, f.field_path)
        ):
            keep.append(f)
            continue
        event = by_node.get(f.node)
        if event is not None and event.inspection is not None:
            event.inspection.tool_failures.append(
                ToolFailure(
                    failure_type="missing_field_guess",
                    field_name=f.field_path or "",
                    severity="warning",
                    evidence=(
                        f"{f.reason} (a guess at the origin; the run reviewer did not confirm it)"
                    ),
                )
            )
    return keep


def _note_unconfirmed(hits: list[Hit]) -> None:
    """File a heuristic hit the reviewer did not confirm as a warning on its step."""
    for hit in hits:
        insp = hit.event.inspection
        if insp is None:
            insp = InspectionResult(
                is_silent_failure=False,
                missing_fields=[],
                empty_fields=[],
                type_mismatches=[],
                severity="warning",
                message="All checks passed",
            )
            hit.event.inspection = insp
        insp.tool_failures.append(
            ToolFailure(
                failure_type=hit.failure_type,
                field_name=hit.field_name,
                severity="warning",
                evidence=hit.evidence,
            )
        )


def _apply_hits(hits: list[Hit]) -> None:
    """Mark each trace-rule hit on its step, the way a subgraph no-op is marked.

    A critical ``ToolFailure`` is what the roll-up, ``argus check`` and
    ``collect_findings`` already read, so no second gate is needed.
    """
    for hit in hits:
        event = hit.event
        insp = event.inspection
        if insp is None:
            insp = InspectionResult(
                is_silent_failure=True,
                missing_fields=[],
                empty_fields=[],
                type_mismatches=[],
                severity="critical",
                message=hit.evidence,
            )
            event.inspection = insp
        elif insp.message == "All checks passed":
            insp.message = hit.evidence
        elif hit.evidence not in insp.message:
            insp.message = f"{insp.message}; {hit.evidence}"
        insp.tool_failures.append(
            ToolFailure(
                failure_type=hit.failure_type,
                field_name=hit.field_name,
                severity="critical",
                evidence=hit.evidence,
            )
        )
        insp.has_tool_failure = True
        insp.is_silent_failure = True
        insp.severity = "critical"
        if event.status != "crashed":
            event.status = "fail"


_MERGED_MIN_STEPS = 3
_MERGED_SHARE = 2 / 3


def _carried(before: Any, after: Any) -> bool:
    """``after`` is ``before`` unchanged, or a list that only grew past it."""
    if after == before:
        return True
    return (
        isinstance(before, list)
        and isinstance(after, list)
        and bool(before)
        and after[: len(before)] == before
    )


def _looks_merged(inp: dict[str, Any], out: dict[str, Any]) -> bool:
    """Every input key survives into the output and at least one is carried.

    Merged state keeps every key the node received and leaves the ones it did
    not write untouched (or, under an append reducer, extended). An update
    names only what the node wrote — ``{"messages": [new]}`` does not start
    with the input's messages, so ``MessagesState`` agents are not caught.
    """
    return set(inp) <= set(out) and any(_carried(inp[k], out[k]) for k in inp)


def _merged_state_steps(events: list[Any]) -> list[str] | None:
    """Names of steps whose output is merged state, if most of them are (#82).

    A trace with every step present can still be skinny: a tracer that logs
    post-merge state instead of the node's return makes ``{}`` unreachable, so
    ``empty_output`` never fires and every run grades clean. One step echoing
    its input is a pass-through node (``{**state, "x": ...}``); two-thirds of
    at least three is a recorder feeding the wrong dict.
    """
    # ponytail: share threshold picked against both matrices + ship_eval with
    # 0 refusals; re-measure when a new adapter lands.
    graded = [
        e
        for e in events
        if isinstance(e.input_state, dict) and e.input_state and isinstance(e.output_dict, dict)
    ]
    if len(graded) < _MERGED_MIN_STEPS:
        return None
    merged = [e.node_name for e in graded if _looks_merged(e.input_state, e.output_dict)]
    if len(merged) / len(graded) < _MERGED_SHARE:
        return None
    return list(dict.fromkeys(merged))


def _refuse(session: ArgusSession, why: str) -> None:
    """Abandon the run rather than grade a recording we cannot trust.

    The session's atexit safety net would otherwise finalize this run on
    interpreter exit and, finding no failures in a trace that never arrived,
    save it as clean — the exact "no findings, so it passed" the brief bans.
    Marking it complete makes that finalize a no-op.
    """
    session._completed = True
    raise IncompleteTraceError(f"{why}. An incomplete recording is not a pass.")
