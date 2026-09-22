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
from argus.llm_tracker import call_from_llm_outputs, usage_from_calls
from argus.models import LLMCallInfo, ToolFailure
from argus.session import ArgusSession

try:  # pragma: no cover - exercised only when langchain-core is absent
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.load import dumpd
    from langchain_core.runnables.base import RunnableBinding

    _HAS_LANGCHAIN = True
except ImportError:  # pragma: no cover
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    dumpd = None  # type: ignore[assignment]
    RunnableBinding = None  # type: ignore[assignment,misc]
    _HAS_LANGCHAIN = False

try:  # pragma: no cover - exercised only when langgraph is absent
    from langgraph.types import Command
except ImportError:  # pragma: no cover
    Command = None  # type: ignore[assignment,misc]

__all__ = ["ArgusRecorder", "IncompleteTraceError"]

# LangGraph's graph sentinels — not nodes anyone wrote.
_SENTINELS = ("__start__", "__end__")


def _node_update(outputs: Any) -> Any:
    """What the node actually wrote, unwrapping a ``Command`` handoff (#88).

    ``Command(goto=..., update={...})`` is the modern handoff idiom — every
    supervisor and multi-agent example emits it — and it is not a dict, so it
    used to fall into the "unreadable shape" branch below and lose the update
    entirely. The step reached the ledger with no update, ``empty_output``
    could not fire, and a declared consumer blamed whoever ran next.

    ``update=None`` (``Command(goto="next")``, routing and nothing else) stays
    ``None``: the node claimed no update, which is not the same as claiming an
    empty one, and flagging it would fail every working supervisor.

    LangGraph also accepts the update as a sequence of key/value pairs. That is
    the same real update wearing a different shape, so it is folded into a dict
    rather than lost the way the ``Command`` itself was. Anything that will not
    fold is returned untouched and lands in the unreadable branch — never an
    exception, because a recorder that raises takes the user's graph down with
    it.
    """
    if Command is None or not isinstance(outputs, Command):
        return outputs
    update = outputs.update
    if update is None or isinstance(update, dict):
        return update
    try:
        return dict(update)
    except (TypeError, ValueError):
        return update


def _command_goto(outputs: Any) -> list[str]:
    """Node names a ``Command`` actually routed to — the edge `get_graph` misses (#110).

    The destination annotation (``-> Command[Literal["write"]]``) is optional in
    LangGraph, and without it the graph reports the node as going straight to
    ``__end__``. A node that looks terminal is exempt from ``empty_output``, so
    the rule stopped firing on exactly the handoff shape #88 was about. The
    route taken is right there on the ``Command``, so it is observed rather
    than inferred.

    ``Send`` carries its target on ``.node``. Sentinels are not filtered here —
    ``_observe_route`` keeps only nodes the graph declares, and ``__end__`` is
    not one, so ``goto=END`` adds no successor and a terminal node stays exempt
    from ``empty_output``. One filter, in the place that has the node list.
    """
    if Command is None or not isinstance(outputs, Command):
        return []
    goto = outputs.goto
    if goto is None:
        return []
    targets = goto if isinstance(goto, (list, tuple)) else [goto]
    names = [getattr(target, "node", target) for target in targets]
    return [name for name in names if isinstance(name, str)]


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
        # node chain run_id -> model calls made anywhere beneath it
        self._llm: dict[UUID, list[LLMCallInfo]] = {}
        # chain run_id -> its parent, so a model call inside `prompt | llm`
        # still finds the node step it ran under
        self._parent_of: dict[UUID, UUID] = {}
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
        # A binding, not `app.with_config(callbacks=[self])` (#87). LangGraph's
        # own `ensure_config` overwrites the callbacks key instead of merging
        # it, so a Pregel's bound callbacks are dropped the moment a caller
        # passes its own — which composition always does. `prompt | app`, a
        # graph used as a tool, a LangServe route: the handler was silently
        # discarded and ARGUS recorded nothing and said nothing.
        # `RunnableBinding` merges through langchain's `merge_configs`, which
        # handles list-plus-manager correctly, and proxies the graph API
        # (`nodes`, `get_graph`, `stream`, `batch`) so the returned object is
        # still the graph as far as callers are concerned.
        return RunnableBinding(bound=app, config={"callbacks": [self]})

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
            consumers=self._consumers,
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
        if node is not None and parent_run_id is None:
            # A node span with no enclosing graph run — the inverse of the
            # skinny trace the matrix covers, with the boundary sampled away
            # instead of the nodes. Making it its own root files a run per node;
            # dropping it is how #87 recorded nothing and said nothing. Neither
            # is a pass, so refuse.
            raise IncompleteTraceError(
                f"node `{node}` reported with no enclosing graph run, so there is "
                "nothing to attribute it to. An incomplete recording is not a pass."
            )
        root = run_id if parent_run_id is None else self._root_of.get(parent_run_id)
        if root is None:
            root = self._adopt_graph_run(node, parent_run_id, inputs)
        if root is None:
            return  # an outer chain that is not ours — someone else's callback
        with self._lock:
            self._root_of[run_id] = root
            if parent_run_id is not None:
                self._parent_of[run_id] = parent_run_id

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

    def _adopt_graph_run(
        self, node: str | None, parent_run_id: UUID | None, inputs: Any
    ) -> UUID | None:
        """Start a run for a graph chain we were never told the start of (#87).

        ``attach`` binds this handler to the graph, but the moment that graph is
        one step of something larger — ``prompt | app``, a graph used as a tool,
        a LangServe route — its chain arrives carrying a parent we never saw.
        Treating "parentless" as the run boundary dropped that chain and then
        every node callback under it: no session, no run file, no verdict, and
        no error either, because ``_finish`` was never reached. A silent pass is
        the one outcome the brief bans, and this one arrived through the front
        door — `argus check` had nothing to grade and said so by saying nothing.

        The enclosing chain of a ``langgraph_node`` callback *is* the graph run,
        whatever ran above it. Adopting it needs no name matching (``LangGraph``
        is not load-bearing and a user can rename it with ``with_config``) and
        no guess about the outer framework.

        Returns the adopted root, or ``None`` for a chain that is not ours —
        the outer sequence in the example above has no node name and belongs to
        whoever built it.
        """
        if node is None or parent_run_id is None:
            return None
        started = self._new_session()
        with self._lock:
            self._root_of[parent_run_id] = parent_run_id
            self._roots[parent_run_id] = started
        self.session = started
        # The first node's input is the graph's state on entry; `capture_state`
        # latches the initial state off the first non-empty snapshot.
        started.capture_state(inputs if isinstance(inputs, dict) else {})
        return parent_run_id

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
            self._parent_of.pop(run_id, None)
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
            llm_calls = self._llm.pop(run_id, [])
            if entry is None:
                return False

            node, input_snap, started = entry
            duration_ms = (time.perf_counter() - started) * 1000

            # `{}` must only ever mean "the node really returned an empty update"
            # — that is the signal. Anything that is not a dict is not an update
            # we can read, so it becomes None (the crash/unknown shape) rather
            # than a fake empty one. A `Command` is unwrapped to the update it
            # carries first, so a handoff is read, not discarded (#88).
            update = _node_update(outputs)
            output_snap = session.capture_output(update) if isinstance(update, dict) else None

            # Before the step is graded, not after: `empty_output` reads the
            # edge map inside `on_node_end` (#110).
            goto = _command_goto(outputs)
            self._observe_route(session, node, goto)

            # Tools go in with the step, not onto the event afterwards: the
            # graders run inside on_node_end, so tools attached later were
            # recorded and never read (#86).
            session.on_node_end(
                node,
                input_snap,
                output_snap,
                duration_ms,
                exc=exc if isinstance(exc, Exception) else None,
                llm_usage=usage_from_calls(llm_calls),
                tool_calls=tools,
                goto=goto,
            )
        return True

    def _observe_route(self, session: ArgusSession, node: str, targets: list[str]) -> None:
        """Merge a route the graph actually took into the edge map (#110).

        Only nodes this graph declares are added, so a ``goto`` can extend the
        topology but never invent it. Subgraph *parents* count: ``_topology``
        returns them apart from ``names`` because their rows are not recorded,
        but they are real destinations, and filtering on ``names`` alone drops
        ``supervise -> docs`` and hands #110 back for every graph that hands off
        into a subgraph.

        One observed route is not the full set of branches — an untaken one
        stays unknown — but "reaches something" beats "terminal", and it is the
        same bargain the trace-file path already makes when it guesses edges
        from step order.
        """
        known = set(self._topology[0]) | self._subgraphs
        edge_map = session.graph_edge_map or {}
        fresh = [t for t in targets if t in known and t not in edge_map.get(node, [])]
        if not fresh:
            return
        merged = {source: list(dests) for source, dests in edge_map.items()}
        merged[node] = merged.get(node, []) + fresh
        session.set_edges(merged)

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
            # F-29: file under the nearest PENDING node step, mirroring the
            # llm re-parent below — a tool invoked inside an inner chain
            # parents to the chain's run id, not the node's, and the exact-key
            # pop at _close_step orphaned those calls. No pending ancestor
            # (tool at graph level): keep the raw parent key.
            owner = parent_run_id
            while owner is not None and owner not in self._pending:
                owner = self._parent_of.get(owner)
            if owner is None:
                owner = parent_run_id
            self._tools.setdefault(owner, []).append(record)

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

    # ── LLM callbacks ───────────────────────────────────────────────────────

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """File one model call's usage and finish reason under its node step.

        The node's own output rarely carries usage, so reading it there records
        zero tokens and never sees a truncation. The call is rebuilt in the shape
        LangChain's tracer exports, so ingest and this path share one parser.
        """
        outputs = response.model_dump()
        for i, batch in enumerate(response.generations):
            for j, generation in enumerate(batch):
                message = getattr(generation, "message", None)
                if message is not None:
                    outputs["generations"][i][j]["message"] = dumpd(message)
        call = call_from_llm_outputs(outputs, kwargs.get("name") or "")
        if call is None:
            return
        with self._lock:
            step = parent_run_id
            while step is not None and step not in self._pending:
                step = self._parent_of.get(step)
            if step is not None:
                self._llm.setdefault(step, []).append(call)

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
