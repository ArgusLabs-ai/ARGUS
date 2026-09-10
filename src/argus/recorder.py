"""Fat-trace recorder — ingest, without wrapping the graph engine.

The pivot's layer 1 (``docs/ARGUS-PIVOT-CONTRIBUTORS.pdf`` §4). ARGUS's old
capture path mutates ``graph.nodes`` before compile and rebinds the compiled
app's ``invoke`` / ``stream`` / ``batch`` (``patcher.py``, ``watcher.py``). This
one does none of that: it rides LangGraph's own callback stream, which already
reports each node by name with the state going in and **the dict the node
returned** — the update, before the framework merges it. That update is the
whole point. A node that searches, discards the result and returns ``{}`` leaves
a full-looking merged state behind; only the update shows the silent no-op, and
only then can blame land on the origin instead of whoever crashes three steps
later.

Usage is the entire public API::

    app = ArgusRecorder().attach(app)
    app.invoke({"query": "..."})

Grading is unchanged: rows go to :mod:`argus.ledger`, then contextual
(:mod:`argus.contextual` — pass ``consumers={"field": ["reader"]}``), then the
existing structure / tool / semantic checks and the LLM judge last, inside
``ArgusSession``. The verdict is ``argus check``.

``ArgusWatcher`` still works and is untouched.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable
from uuid import UUID

from argus.contextual import ConsumerMap, contextual_findings
from argus.ledger import build_ledger
from argus.models import Finding, LLMInvestigationConfig
from argus.session import ArgusSession

try:  # pragma: no cover - exercised only when langchain-core is absent
    from langchain_core.callbacks import BaseCallbackHandler

    _HAS_LANGCHAIN = True
except ImportError:  # pragma: no cover
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _HAS_LANGCHAIN = False

__all__ = ["ArgusRecorder", "IncompleteTraceError"]

# LangGraph's graph sentinels — not nodes anyone wrote.
_SENTINELS = ("__start__", "__end__")


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
    structural field check degrades to ``unannotated_successors`` instead of
    quietly inventing a contract. That contract is layer 3
    (:mod:`argus.contextual`).
    """

    def _node(state):  # type: ignore[no-untyped-def]  # unannotated on purpose
        raise RuntimeError(f"{name} is a trace placeholder and is never called")

    _node.__name__ = name
    return _node


def _reducer_fields(app: Any) -> dict[str, Any]:
    """Reducers declared on the state schema, e.g. ``Annotated[list, operator.add]``.

    Without these, a fan-in field looks overwritten instead of accumulated and
    the state successors are graded against is wrong. `app.builder` is public;
    reading it is not patching it. Best-effort — an app without one still
    records, it just grades fan-in with a plain overlay.
    """
    builder = getattr(app, "builder", None)
    if builder is None:
        return {}
    try:
        from argus.utils.type_introspection import extract_reducer_fields

        return extract_reducer_fields(builder)
    except Exception:
        return {}


class ArgusRecorder(BaseCallbackHandler):
    """Records a fat trace of one LangGraph run and grades it."""

    # Surface our own bugs instead of letting langchain swallow them.
    raise_error = True

    def __init__(
        self,
        *,
        validators: dict[str, Callable[[dict[str, Any]], tuple[bool, str]]] | None = None,
        strict: bool = False,
        semantic_judge: bool = False,
        max_field_size: int = 50_000,
        consumers: ConsumerMap | None = None,
    ) -> None:
        if not _HAS_LANGCHAIN:
            raise ImportError(
                "ArgusRecorder needs langchain-core. Install it with: pip install langchain-core"
            )
        self._validators = validators or {}
        self._strict = strict
        self._semantic_judge = semantic_judge
        self._max_field_size = max_field_size
        # Declared `field -> [reader nodes]`; a trace cannot tell us who reads what.
        self._consumers = consumers

        self.session: ArgusSession | None = None
        self._lock = threading.Lock()
        # run_id -> (node name, input snapshot, start time)
        self._pending: dict[UUID, tuple[str, dict[str, Any], float]] = {}
        # node chain run_id -> tool records recorded under it
        self._tools: dict[UUID, list[dict[str, Any]]] = {}
        # tool run_id -> that tool's own record, so concurrent tools don't cross
        self._tool_owner: dict[UUID, dict[str, Any]] = {}
        self._root_run_id: UUID | None = None

    # ── attach ──────────────────────────────────────────────────────────────

    def attach(self, app: Any) -> Any:
        """Bind the recorder to a compiled graph. Returns the app to invoke.

        Reads topology through the public ``get_graph()`` and returns
        ``app.with_config(callbacks=[self])``. Nothing is patched or mutated.
        """
        graph = app.get_graph()
        node_names = [n for n in graph.nodes if n not in _SENTINELS]

        edge_map: dict[str, list[str]] = {}
        conditional_sources: set[str] = set()
        for edge in graph.edges:
            if edge.source in _SENTINELS or edge.target in _SENTINELS:
                continue
            edge_map.setdefault(edge.source, []).append(edge.target)
            if getattr(edge, "conditional", False):
                conditional_sources.add(edge.source)

        session = ArgusSession(
            max_field_size=self._max_field_size,
            validators=self._validators,
            strict=self._strict,
            # Explicit config (never None) so the session does not auto-enable the
            # judge just because a provider key happens to be around. The judge is
            # last, and opt-in.
            llm_investigation=LLMInvestigationConfig(
                enabled=self._semantic_judge,
                always_investigate=self._semantic_judge,
                semantic_check=self._semantic_judge,
            ),
        )
        session.set_node_names(node_names)
        session.set_edges(edge_map)
        session.set_conditional_sources(conditional_sources)
        session.node_fn_registry = {name: _placeholder_node(name) for name in node_names}
        session.reducer_fields = _reducer_fields(app)
        # The recorder owns finalize: the ledger and contextual layers run over
        # the complete trace, before the run is graded and saved.
        session._defer_auto_finalize = True

        self.session = session
        return app.with_config(callbacks=[self])

    # ── chain callbacks ─────────────────────────────────────────────────────

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        session = self._require_session()
        node = (metadata or {}).get("langgraph_node")

        if node is None:
            # The graph run itself (no node name, no parent) — the run boundary.
            if parent_run_id is None and self._root_run_id is None:
                self._root_run_id = run_id
                session.capture_state(inputs if isinstance(inputs, dict) else {})
            return

        input_snap = session.capture_state(inputs if isinstance(inputs, dict) else {})
        with self._lock:
            self._pending[run_id] = (node, input_snap, time.perf_counter())
        session.on_node_start(node, input_snap)

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if self._close_step(run_id, outputs, exc=None):
            return
        if run_id == self._root_run_id:
            self._finish()

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if self._close_step(run_id, None, exc=error):
            return
        if run_id == self._root_run_id:
            self._finish()

    def _close_step(self, run_id: UUID, outputs: Any, exc: BaseException | None) -> bool:
        """Record one node step. Returns True if ``run_id`` was a node."""
        session = self._require_session()
        # Held across on_node_end so the step index we file tools under is the
        # one this step actually gets. Parallel fan-out runs these callbacks on
        # separate threads and the session assigns indices under its own lock.
        with self._lock:
            entry = self._pending.pop(run_id, None)
            tools = self._tools.pop(run_id, [])
            if entry is None:
                return False

            node, input_snap, started = entry
            duration_ms = (time.perf_counter() - started) * 1000

            # `{}` must only ever mean "the node really returned an empty update"
            # — that is the signal. Anything that is not a dict is not an update
            # we can read, so it becomes None (the crash/unknown shape) rather
            # than a fake empty one.
            output_snap = session.capture_output(outputs) if isinstance(outputs, dict) else None

            session.on_node_end(
                node,
                input_snap,
                output_snap,
                duration_ms,
                exc=exc if isinstance(exc, Exception) else None,
            )
            # Tools go on the event so they survive the run file's asdict
            # round-trip. Only recorder callbacks append events, and this lock
            # is held across on_node_end, so [-1] is the step just recorded.
            if tools and session._events:
                session._events[-1].tool_calls = tools
        return True

    # ── tool callbacks ──────────────────────────────────────────────────────

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is None:
            return
        record = {
            "name": (serialized or {}).get("name", "tool"),
            "input": input_str,
            "output": None,
            "error": None,
        }
        with self._lock:
            self._tool_owner[run_id] = record
            self._tools.setdefault(parent_run_id, []).append(record)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._close_tool(run_id, output=output, error=None)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._close_tool(run_id, output=None, error=repr(error))

    def _close_tool(self, run_id: UUID, output: Any, error: str | None) -> None:
        # Closed by the tool's own run_id, not "the last one started under this
        # node" — a node may have several tools in flight at once.
        with self._lock:
            record = self._tool_owner.pop(run_id, None)
            if record is None:
                return
            record["output"] = output
            record["error"] = error

    # ── the layer chain ─────────────────────────────────────────────────────

    def _finish(self) -> None:
        """Ledger → contextual → structure/tools + semantic → judge → verdict."""
        session = self._require_session()

        with self._lock:
            unfinished = sorted(node for node, _, _ in self._pending.values())
        if unfinished:
            self._refuse(
                session,
                f"steps started but never reported an update: {', '.join(unfinished)}",
            )
        if not session._events:
            self._refuse(session, "no steps were recorded — the trace is empty")

        ledger = build_ledger(session._events, session._initial_state)
        self._blame_origins(session, contextual_findings(ledger, self._consumers))

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

    @staticmethod
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
            insp.message = finding.reason
            event.status = "fail"

    @staticmethod
    def _refuse(session: ArgusSession, why: str) -> None:
        """Abandon the run rather than grade a recording we cannot trust.

        The session's atexit safety net would otherwise finalize this run on
        interpreter exit and, finding no failures in a trace that never arrived,
        save it as clean — the exact "no findings, so it passed" the brief bans.
        Marking it complete makes that finalize a no-op.
        """
        session._completed = True
        raise IncompleteTraceError(f"{why}. An incomplete recording is not a pass.")

    def _require_session(self) -> ArgusSession:
        if self.session is None:
            raise RuntimeError("ArgusRecorder.attach(app) must be called before invoking the app")
        return self.session
