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

The LLM judge is on by default when a key is configured (``argus key set`` or
``argus login``) and off otherwise — no extra flag. Force it either way with
``ArgusRecorder(semantic_judge=True|False)``.

``ArgusWatcher`` still works and is untouched.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable
from uuid import UUID

from argus.contextual import ConsumerMap
from argus.grading import IncompleteTraceError, finish, new_session
from argus.models import ToolFailure
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


def _subgraph_shape(app: Any) -> tuple[dict[str, list[str]], set[str], set[str]]:
    """``{subgraph: [its inner nodes]}``, the ones with a successor, outer keys.

    Needed for the one question no inner step can answer on its own: did the
    subgraph contribute anything to the **parent** graph's state? An inner node
    writing an inner-only key returns a perfectly non-empty update, so
    ``empty_output`` stays quiet, while the parent state gains nothing and the
    node after the subgraph reads ``None`` (#89). Keys the outer graph has are
    ``builder.channels`` — the same public ``builder`` :func:`_reducer_fields`
    already reads.

    Best-effort: an app that exposes none of this yields empty sets, which only
    switches the check off.
    """
    try:
        xray = app.get_graph(xray=True)
    except Exception:  # pragma: no cover - older/patched LangGraph
        return {}, set(), set()

    inner: dict[str, list[str]] = {}
    for node_id in xray.nodes:
        if ":" not in node_id:
            continue
        parent, _, _ = node_id.partition(":")
        name = _bare(node_id)
        if name not in _SENTINELS:
            inner.setdefault(parent, []).append(name)

    with_successors = {
        edge.source
        for edge in app.get_graph().edges
        if edge.source in inner and _bare(edge.target) not in _SENTINELS
    }
    outer_keys = set(getattr(getattr(app, "builder", None), "channels", None) or {})
    return inner, with_successors, outer_keys


def _bare(node_id: str) -> str:
    """``child:retrieve`` → ``retrieve``.

    ``get_graph(xray=True)`` qualifies a subgraph's nodes with their parent, but
    the callback stream reports ``langgraph_node`` as the bare name. The trace
    is what we have to match against, so the bare name is the key.

    ponytail: two subgraphs that each contain a `retrieve` collapse onto one
    entry. Not recoverable here — the callbacks report both as `retrieve`, so
    the ambiguity is in the trace, not in this line. Fix it upstream (qualified
    names in the trace) if it ever bites.
    """
    return node_id.rsplit(":", 1)[-1]


def _topology(app: Any) -> tuple[list[str], dict[str, list[str]], set[str], set[str]]:
    """Node names, ``{node: [successors]}``, conditional sources, subgraph parents.

    Read with ``xray=True`` so nodes *inside* a subgraph are known too. Without
    it a subgraph is one opaque ``child`` node, its inner nodes are absent from
    ``node_fn_registry``, and every one of them looks like it has no successors
    — which silently exempts them from the ``empty_output`` rule. A silent
    no-op nested one level down then graded clean.

    A subgraph's *parent* (``documents`` in ``documents:ocr``) is reported by the
    callbacks as a node in its own right, but its "update" is the subgraph's whole
    merged state — a dozen keys other nodes wrote. That is the skinny-trace-posing-
    as-fat shape (#82), produced by our own recorder: it double-reports every
    inner finding on the parent, blames it for fields it never wrote, and makes
    ``contextual._wrote()`` true for it on every field, which can hide a real
    drop. So the parents come back as their own set — the recorder records the
    inner nodes and skips the parent row. Their edges stay in the map: the node
    before the subgraph still has a successor waiting on it.
    """
    try:
        graph = app.get_graph(xray=True)
    except Exception:  # pragma: no cover - older/patched LangGraph
        graph = app.get_graph()

    parents = {n.split(":", 1)[0] for n in graph.nodes if ":" in n}
    names = [
        _bare(n) for n in graph.nodes if _bare(n) not in _SENTINELS and _bare(n) not in parents
    ]
    for outer in app.get_graph().nodes:
        if outer not in _SENTINELS and outer not in names and outer not in parents:
            names.append(outer)

    edge_map: dict[str, list[str]] = {}
    conditional_sources: set[str] = set()
    for edge in graph.edges:
        source, target = _bare(edge.source), _bare(edge.target)
        if source in _SENTINELS or target in _SENTINELS:
            continue
        edge_map.setdefault(source, []).append(target)
        if getattr(edge, "conditional", False):
            conditional_sources.add(source)
    return names, edge_map, conditional_sources, parents


class ArgusRecorder(BaseCallbackHandler):
    """Records a fat trace of one LangGraph run and grades it."""

    # Surface our own bugs instead of letting langchain swallow them.
    raise_error = True

    def __init__(
        self,
        *,
        validators: dict[str, Callable[[dict[str, Any]], tuple[bool, str]]] | None = None,
        strict: bool = False,
        semantic_judge: bool | None = None,
        max_field_size: int = 50_000,
        consumers: ConsumerMap | None = None,
    ) -> None:
        if not _HAS_LANGCHAIN:
            raise ImportError(
                "ArgusRecorder needs langchain-core. Install it with: pip install langchain-core"
            )
        self._validators = validators or {}
        self._strict = strict
        # None = auto: on when a key/login is available, off otherwise (resolved
        # at attach). True/False force it either way regardless of key state.
        self._semantic_judge = semantic_judge
        self._max_field_size = max_field_size
        # Declared `field -> [reader nodes]`; a trace cannot tell us who reads what.
        self._consumers = consumers

        # The most recently started run's session. One attach can serve many
        # runs (a served app, a loop, `.batch()`), so the recorder keeps one
        # session *per run* and this is only the latest — read `run_ids` when
        # several runs came out of one attach.
        self.session: ArgusSession | None = None
        self.run_ids: list[str] = []
        self._lock = threading.Lock()
        # run_id -> (node name, input snapshot, start time)
        self._pending: dict[UUID, tuple[str, dict[str, Any], float]] = {}
        # node chain run_id -> tool records recorded under it
        self._tools: dict[UUID, list[dict[str, Any]]] = {}
        # tool run_id -> that tool's own record, so concurrent tools don't cross
        self._tool_owner: dict[UUID, dict[str, Any]] = {}
        # One session per graph run, keyed by that run's root callback id, plus
        # every callback id's route back to its root. `.batch()` runs its items
        # on separate threads with interleaved callbacks; without this they fold
        # into one notebook and the running state is a merge of two different
        # inputs, which is worse than no verdict.
        self._roots: dict[UUID, ArgusSession] = {}
        self._root_of: dict[UUID, UUID] = {}
        # Every chain run_id -> the node name it belongs to, so a step's inner
        # runnables are recognised however deeply they nest.
        self._node_of: dict[UUID, str] = {}
        self._attached = False
        # Set at attach; every per-run session is built from them.
        self._topology: tuple[list[str], dict[str, list[str]], set[str]] = ([], {}, set())
        self._subgraphs: set[str] = set()
        # {subgraph: [inner nodes]}, those with a node waiting after them, and
        # the keys the outer graph actually has — see _blame_barren_subgraphs.
        self._subgraph_nodes: dict[str, list[str]] = {}
        self._subgraphs_with_successors: set[str] = set()
        self._outer_keys: set[str] = set()
        self._reducers: dict[str, Any] = {}
        self._judge = False

    # ── attach ──────────────────────────────────────────────────────────────

    def attach(self, app: Any) -> Any:
        """Bind the recorder to a compiled graph. Returns the app to invoke.

        Reads topology through the public ``get_graph(xray=True)`` (see
        :func:`_topology`, so subgraph nodes are graded too) and returns
        ``app.with_config(callbacks=[self])``. Nothing is patched or mutated.

        The returned app is reusable: every ``invoke`` / ``stream`` / ``batch``
        item gets its own session, its own run file and its own verdict.
        """
        names, edges, conditionals, self._subgraphs = _topology(app)
        self._topology = (names, edges, conditionals)
        (
            self._subgraph_nodes,
            self._subgraphs_with_successors,
            self._outer_keys,
        ) = _subgraph_shape(app)
        self._reducers = _reducer_fields(app)
        self._judge = self._resolve_judge()
        self._attached = True
        return app.with_config(callbacks=[self])

    def _new_session(self) -> ArgusSession:
        """A session for one graph run — the state the old ``attach`` set up."""
        node_names, edge_map, conditional_sources = self._topology
        return new_session(
            node_names,
            edge_map,
            conditional_sources,
            self._reducers,
            judge=self._judge,
            validators=self._validators,
            strict=self._strict,
            max_field_size=self._max_field_size,
            state_keys=sorted(self._outer_keys),
        )

    def _resolve_judge(self) -> bool:
        """Decide whether the LLM judge runs for this attach.

        Explicit ``True``/``False`` wins. Left unset (``None``), the judge turns
        on when an LLM path is usable — a BYOK key (``argus key set``) or a login
        (``argus login``) — and stays off otherwise. Setting a key is intent
        enough; a second opt-in flag is friction. With no key the judge would
        only skip anyway, so defaulting it on there would just report a check
        that never ran.
        """
        if self._semantic_judge is not None:
            return self._semantic_judge
        from argus.llm_proxy import is_available

        return is_available()

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
        self._require_attached()
        node = (metadata or {}).get("langgraph_node")

        # Route this callback to the run it belongs to. A parentless chain *is*
        # a run boundary; everything else inherits its parent's root.
        root = run_id if parent_run_id is None else self._root_of.get(parent_run_id)
        if root is None:
            return  # a callback we cannot attribute to any run we started
        with self._lock:
            self._root_of[run_id] = root

        if node is None:
            # The graph run itself (no node name, no parent) — the run boundary.
            if run_id == root:
                started = self._new_session()
                with self._lock:
                    self._roots[root] = started
                self.session = started
                started.capture_state(inputs if isinstance(inputs, dict) else {})
            return

        session = self._roots.get(root)
        if session is None:
            return

        with self._lock:
            # Which node, if any, the enclosing chain already belongs to —
            # tracked for *every* chain, recorded or not. Matching only against
            # open `_pending` steps meant a suppressed inner chain vanished from
            # the lineage and its own children looked like fresh visits: a
            # `create_react_agent` filed four `agent` rows for two turns, two of
            # them with no update at all, and the phantoms pushed the real rows
            # into `retried` — a status `argus check` skips. Verified against
            # langgraph 0.6.11 / prebuilt react.
            enclosing = self._node_of.get(parent_run_id) if parent_run_id else None
            self._node_of[run_id] = node
        if node in self._subgraphs:
            # The subgraph's own parent chain — its "update" is the merged state
            # of everything inside it (see _subgraph_parents). Its nodes are
            # recorded individually; this row would only double-count them.
            return
        if enclosing == node:
            # A chain nested inside the step we are already recording, carrying
            # that same node's name: LangGraph's own inner runnable, not a
            # second visit. Matching on the name rather than the `seq:step:N`
            # tag keeps a real subgraph's inner nodes (different names)
            # recorded. A subgraph node sharing its parent's name is folded into
            # the parent — the same collision `_bare()` documents.
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
        self._end(run_id, outputs, exc=None)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._end(run_id, None, exc=error)

    def _end(self, run_id: UUID, outputs: Any, exc: BaseException | None) -> None:
        """Close whatever ``run_id`` was: a node step, a run, or neither."""
        with self._lock:
            root = self._root_of.pop(run_id, None)
            self._node_of.pop(run_id, None)
        if root is None:
            return
        session = self._roots.get(root)
        if session is None:
            return
        if self._close_step(session, run_id, outputs, exc=exc):
            return
        if run_id == root:
            with self._lock:
                self._roots.pop(root, None)
            self._finish(session, root)

    def _close_step(
        self, session: ArgusSession, run_id: UUID, outputs: Any, exc: BaseException | None
    ) -> bool:
        """Record one node step. Returns True if ``run_id`` was a node."""
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

            # Tools go in with the step, not onto the event afterwards: the
            # graders run inside on_node_end, so tools attached later were
            # recorded and never read (#86).
            session.on_node_end(
                node,
                input_snap,
                output_snap,
                duration_ms,
                exc=exc if isinstance(exc, Exception) else None,
                tool_calls=tools,
            )
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

    def _finish(self, session: ArgusSession, root: UUID) -> None:
        """Grade one finished run (:func:`argus.grading.finish`)."""
        with self._lock:
            # Only this run's steps. Another `.batch()` item may still be mid
            # flight on another thread; its open steps are not this run's gap.
            unfinished = sorted(
                node
                for rid, (node, _, _) in self._pending.items()
                if self._root_of.get(rid) == root
            )
        self._blame_barren_subgraphs(session)
        finish(session, self._consumers, unfinished)
        self.run_ids.append(session.run_id)

    def _blame_barren_subgraphs(self, session: ArgusSession) -> None:
        """Fail a subgraph that ran and left the parent state untouched (#89).

        Every inner node can return a non-empty update and the subgraph still
        contribute nothing outward, because an inner-only key never reaches the
        parent graph. ``empty_output`` asks "was this update empty?", which is
        the wrong question one level down; this asks "did any of it survive into
        the outer state?".

        Subgraph-level on purpose. Blaming each inner node would fire on the
        normal shape where an early node writes a scratch key purely to feed a
        later one — real work that legitimately contributes nothing outward. The
        finding lands on the first inner step, which is where the existing
        all-empty case already blames, and is skipped when that step is flagged
        already so one no-op is not reported twice.
        """
        if not self._outer_keys:
            return

        for parent in self._subgraphs_with_successors:
            inner = set(self._subgraph_nodes.get(parent, []))
            # Every visit, not the first one each: a subgraph on a loop edge can
            # come up dry on pass one and contribute on pass two, and flagging
            # that would be a false positive.
            steps = [event for event in session._events if event.node_name in inner]
            if not steps:
                continue
            if any(set(step.output_dict or {}) & self._outer_keys for step in steps):
                continue
            # Earliest inner node, but its *last* visit. Status cannot be read
            # here — `retried` is assigned later, in finalize, which demotes
            # every visit but the last. Blaming the first visit of a subgraph on
            # a loop edge therefore parks the finding on a step that
            # `check.evaluate_run` and `collect_findings` both drop, and the run
            # goes out clean. Keeping the node but taking its final visit holds
            # origin blame and stays visible.
            first_node = steps[0].node_name
            origin = [step for step in steps if step.node_name == first_node][-1]
            if origin.inspection is None or origin.inspection.has_tool_failure:
                continue
            origin.inspection.tool_failures.append(
                ToolFailure(
                    failure_type="subgraph_no_contribution",
                    field_name="_output",
                    severity="critical",
                    evidence=(
                        f"subgraph `{parent}` ran and wrote nothing the parent graph can "
                        f"see — every field it produced is internal to it, so the node "
                        f"after it reads the state unchanged"
                    ),
                )
            )
            origin.inspection.has_tool_failure = True
            origin.inspection.is_silent_failure = True
            origin.inspection.severity = "critical"
            origin.status = "fail"

    def _require_attached(self) -> None:
        if not self._attached:
            raise RuntimeError("ArgusRecorder.attach(app) must be called before invoking the app")
