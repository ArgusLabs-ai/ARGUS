from __future__ import annotations

import importlib
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from argus.models import RunRecord
from argus.storage import load_run
from argus.utils.serializer import safe_deserialize


def _smart_merge(state: dict, partial: dict) -> dict:
    """Merge partial output into state, respecting list-append semantics.

    LangGraph reducers (e.g. ``Annotated[list, operator.add]``) append list
    outputs instead of overwriting.  The naive ``{**state, **partial}`` merge
    destroys this: a node returning ``{"messages": [new_msg]}`` replaces
    the entire messages list instead of appending.

    Heuristic: if a key exists in both *state* and *partial* and both
    values are lists, we concatenate them (matching ``operator.add``).
    For all other types we overwrite (matching ``operator.setitem``).
    """
    merged = dict(state)
    for key, value in partial.items():
        if key in merged and isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = merged[key] + value
        else:
            merged[key] = value
    return merged


def _make_llm_inv_config():
    """Return LLMInvestigationConfig if LLM calls are available, else None."""
    from argus.llm_proxy import is_available

    if not is_available():
        return None
    try:
        from argus.models import LLMInvestigationConfig

        return LLMInvestigationConfig(enabled=True, always_investigate=False)
    except Exception:
        return None


class ReplayEngine:
    """Loads a saved run's state at a specific node and re-runs the pipeline from there.

    Supports two modes:
      1. Factory-free (preferred): uses stored node_fn_refs to import each node
         function directly and replays via ArgusSession — no factory needed.
      2. Factory mode (fallback): uses an app_factory callable to rebuild the
         full LangGraph graph. Required only when node_fn_refs are missing
         (e.g., old runs recorded before auto-capture was added).
    """

    def __init__(self, max_field_size: int = 50_000) -> None:
        self._max_field_size = max_field_size

    @staticmethod
    def _patch_input(
        input_state: dict[str, Any],
        patch: dict[str, Any] | None,
        create_missing: bool,
        node_name: str,
    ) -> dict[str, Any]:
        """Apply an optional state patch to a recorded input state.

        Patching happens on the raw recorded dict *before* deserialization, so
        typed states (pydantic / dataclass / TypedDict) are validated by the
        deserializer against the patched values rather than bypassed.
        """
        if not patch:
            return input_state
        from argus.state_patch import PatchError, apply_patch

        try:
            return apply_patch(input_state, patch, create_missing=create_missing)
        except PatchError as exc:
            raise PatchError(
                f"cannot patch input state for node '{node_name}': {exc}"
            ) from exc

    def replay_node(
        self,
        run_id: str,
        node_name: str,
        state_type: type | None = None,
        patch: dict[str, Any] | None = None,
        create_missing: bool = False,
    ) -> str:
        """Re-execute a single node in isolation using its original input state.

        Imports the node function via stored node_fn_refs, runs it with the
        original input_state, and records a single-step RunRecord.

        Args:
            run_id: the run-id (or prefix) to replay from
            node_name: the node to re-run
            state_type: optional state type class for deserialization
            patch: optional state patch applied to the node's recorded input
                before replaying (see :mod:`argus.state_patch`)
            create_missing: allow the patch to create keys that do not exist

        Returns:
            new run-id of the single-node replay run
        """
        from argus.session import ArgusSession

        record = load_run(run_id)

        step = next((e for e in record.steps if e.node_name == node_name), None)
        if step is None:
            available = [e.node_name for e in record.steps]
            raise ValueError(
                f"Node '{node_name}' not found in run '{run_id}'. Available nodes: {available}"
            )

        if not record.node_fn_refs or node_name not in record.node_fn_refs:
            # Auto-locate source files before giving up
            record = self._auto_locate(record)
            if not record.node_fn_refs or node_name not in record.node_fn_refs:
                raise ValueError(
                    f"No stored function reference for node '{node_name}'. "
                    "Re-record the run with the latest argus to enable single-node replay."
                )

        raw_state = self._patch_input(step.input_state, patch, create_missing, node_name)
        state = safe_deserialize(raw_state, state_type)
        fp = (record.node_fn_paths or {}).get(node_name)
        fn = _import_fn(record.node_fn_refs[node_name], file_path=fp)

        session = ArgusSession(
            max_field_size=self._max_field_size,
            llm_investigation=_make_llm_inv_config(),
        )
        session.set_node_names([node_name])
        session.set_edges({})
        session.parent_run_id = record.run_id
        session.replay_from_step = node_name
        session.state_patch = patch or None
        session.node_fn_refs = {node_name: record.node_fn_refs[node_name]}

        wrapped = session.wrap(node_name, fn)
        wrapped(state)

        session.finalize()
        return session.run_id

    def replay(
        self,
        run_id: str,
        from_node: str,
        app_factory: Callable[[], Any] | None = None,
        state_type: type | None = None,
        patch: dict[str, Any] | None = None,
        create_missing: bool = False,
    ) -> str:
        """Replay a run starting from from_node.

        Args:
            run_id: the run-id (or prefix) to replay from
            from_node: the node name to start replay from
            app_factory: optional — only needed if the run has no stored node_fn_refs
            state_type: optional state type class for deserialization
            patch: optional state patch applied to from_node's recorded input
                before replaying (see :mod:`argus.state_patch`).  Upstream
                nodes stay frozen, so only the resumed trajectory changes.
            create_missing: allow the patch to create keys that do not exist

        Returns:
            new run-id of the replay run
        """
        record = load_run(run_id)

        # find the step for from_node
        step = next((e for e in record.steps if e.node_name == from_node), None)
        if step is None:
            available = [e.node_name for e in record.steps]
            raise ValueError(
                f"Node '{from_node}' not found in run '{run_id}'. Available nodes: {available}"
            )

        # build frozen outputs ONLY for nodes before from_node
        frozen_map: dict[str, list[Any]] = defaultdict(list)
        for s in record.steps:
            if s.node_name == from_node:
                break
            if s.output_dict is not None:
                frozen_map[s.node_name].append(s.output_dict)

        # apply the optional patch to the raw recorded state, then deserialize
        raw_state = self._patch_input(step.input_state, patch, create_missing, from_node)
        state = safe_deserialize(raw_state, state_type)

        # Try factory-free replay first, fall back to factory mode
        if record.node_fn_refs:
            return self._replay_direct(record, from_node, state, frozen_map, patch)

        # Auto-locate source files before requiring a factory
        record = self._auto_locate(record)
        if record.node_fn_refs:
            return self._replay_direct(record, from_node, state, frozen_map, patch)

        if app_factory is not None:
            return self._replay_with_factory(
                record,
                from_node,
                state,
                frozen_map,
                app_factory,
                patch,
            )

        raise ValueError(
            "Cannot replay: this run has no stored node function references "
            "and no app_factory was provided. Re-record the run with the "
            "latest argus version to enable factory-free replay."
        )

    @staticmethod
    def _auto_locate(record: RunRecord) -> RunRecord:
        """Attempt post-hoc source resolution and update the record."""
        try:
            from argus.source_locator import derive_node_fn_refs, locate_node_sources
            from argus.storage import save_run
        except ImportError:
            return record

        resolved = locate_node_sources(record, use_llm=True)
        if not resolved:
            return record

        record.node_fn_paths = resolved
        refs = derive_node_fn_refs(resolved)
        if refs:
            record.node_fn_refs = refs
            save_run(record)
        return record

    def _replay_direct(
        self,
        record: RunRecord,
        from_node: str,
        state: Any,
        frozen_map: dict[str, list[Any]],
        patch: dict[str, Any] | None = None,
    ) -> str:
        """Replay by importing node functions directly — no factory needed."""
        from argus.session import ArgusSession

        # Import each node function from the stored refs
        node_fns: dict[str, Callable] = {}
        fn_paths = record.node_fn_paths or {}
        for name, ref in record.node_fn_refs.items():
            node_fns[name] = _import_fn(ref, file_path=fn_paths.get(name))

        # Build execution order from the original run's steps (deduplicated, preserves order)
        seen: set[str] = set()
        execution_order: list[str] = []
        reached_from = False
        for s in record.steps:
            if s.node_name == from_node:
                reached_from = True
            if reached_from and s.node_name not in seen:
                seen.add(s.node_name)
                execution_order.append(s.node_name)

        session = ArgusSession(
            max_field_size=self._max_field_size,
            llm_investigation=_make_llm_inv_config(),
        )
        session.set_node_names(record.graph_node_names)
        session.set_edges(record.graph_edge_map)
        session.parent_run_id = record.run_id
        session.replay_from_step = from_node
        session.state_patch = patch or None
        session.frozen_outputs = dict(frozen_map)
        session.node_fn_refs = record.node_fn_refs

        # Store original function refs for inspector type-hint analysis
        session.node_fn_registry = node_fns

        # Wrap and run each node in the recorded execution order.
        # LangGraph nodes return partial state updates (only changed keys),
        # so we merge each output into the running state before passing to
        # the next node — mimicking what LangGraph's reducer does.
        wrapped = {name: session.wrap(name, fn) for name, fn in node_fns.items()}

        # Load recorded HTTP interactions for deterministic replay
        http_interactions = None
        try:
            from argus.http_recorder import load_http_interactions

            http_interactions = load_http_interactions(record.run_id)
        except Exception:
            pass

        http_ctx = None
        if http_interactions:
            try:
                from argus.http_recorder import playback_http

                http_ctx = playback_http(http_interactions)
                http_ctx.__enter__()
            except Exception:
                pass

        try:
            for node_name in execution_order:
                fn = wrapped.get(node_name)
                if fn is None:
                    raise ValueError(
                        f"Node '{node_name}' has no stored function reference. "
                        f"Available: {list(record.node_fn_refs.keys())}"
                    )
                partial = fn(state)
                if isinstance(partial, dict) and isinstance(state, dict):
                    state = _smart_merge(state, partial)
                else:
                    state = partial
        finally:
            if http_ctx is not None:
                try:
                    http_ctx.__exit__(None, None, None)
                except Exception:
                    pass

        session.finalize()
        return session.run_id

    def _replay_with_factory(
        self,
        record: RunRecord,
        from_node: str,
        state: Any,
        frozen_map: dict[str, list[Any]],
        app_factory: Callable[[], Any],
        patch: dict[str, Any] | None = None,
    ) -> str:
        """Replay using a factory function (legacy fallback)."""
        from argus.watcher import ArgusWatcher

        try:
            graph = app_factory()
        except Exception as exc:
            raise ValueError(
                f"app_factory raised an error: {exc}. "
                "The factory should be a zero-arg function that returns a "
                "StateGraph or CompiledGraph."
            ) from exc

        if graph is None:
            raise ValueError(
                "app_factory returned None. It should return a LangGraph "
                "StateGraph or CompiledGraph."
            )

        if isinstance(graph, dict):
            raise ValueError(
                "app_factory returned a dict instead of a StateGraph. "
                "This usually means the factory runs the graph and returns "
                "its result. The factory should return the StateGraph BEFORE "
                "calling .compile() or .invoke().\n\n"
                "  def build_graph():\n"
                "      graph = StateGraph(MyState)\n"
                "      graph.add_node(...)\n"
                "      return graph  # not graph.compile().invoke()\n\n"
                "Set the correct factory via argus ui --app module:fn or in "
                "the dashboard settings."
            )

        # Unwrap compiled graphs
        if hasattr(graph, "invoke") and hasattr(graph, "graph"):
            graph = graph.graph

        if hasattr(graph, "nodes") and not hasattr(graph, "invoke"):
            watcher = ArgusWatcher(max_field_size=self._max_field_size, investigate=True)
            watcher.watch(graph)
            if watcher._session is not None:
                watcher._session.parent_run_id = record.run_id
                watcher._session.replay_from_step = from_node
                watcher._session.state_patch = patch or None
                watcher._session.frozen_outputs = dict(frozen_map)
            new_run_id = watcher.run_id
            app = graph.compile()
        else:
            raise ValueError(
                "app_factory must return a LangGraph StateGraph or CompiledGraph. "
                "Got: " + type(graph).__name__
            )

        app.invoke(state)

        if watcher is not None:
            watcher.finalize()

        return new_run_id


def _strip_line_number(path: str) -> str:
    """Remove trailing ``:line_number`` from a ``file:line`` path.

    Handles both old format (``src/foo.py``) and new format
    (``src/foo.py:42``).
    """
    if ":" in path and path.rsplit(":", 1)[1].isdigit():
        return path.rsplit(":", 1)[0]
    return path


def _import_fn(ref: str, file_path: str | None = None) -> Callable:
    """Import a function from a 'module:qualname' reference.

    Handles both simple names (module:func) and nested qualnames
    (module:Class.method).  When *file_path* is provided, it is used as a
    last-resort fallback via ``spec_from_file_location`` — no ``__init__.py``
    required in parent directories.
    """
    # Strip line number suffix from file_path (e.g. "src/foo.py:42" → "src/foo.py")
    if file_path:
        file_path = _strip_line_number(file_path)

    if ":" not in ref:
        raise ValueError(f"Invalid function ref '{ref}' — expected 'module:qualname'")

    module_path, qualname = ref.rsplit(":", 1)

    # Ensure cwd is importable
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        # Force-reload so replay always picks up the user's latest code fixes.
        # Without this, Python's module cache (sys.modules) would serve the
        # stale version from when argus ui first imported the module.
        if module_path in sys.modules:
            module = importlib.reload(sys.modules[module_path])
        else:
            module = importlib.import_module(module_path)
    except ImportError:
        # Module not on sys.path — search for the .py file or package under CWD
        # (handles pipelines run from subdirectories)
        top_level = module_path.split(".")[0]
        found = list(Path.cwd().rglob(f"{top_level}.py"))
        # Also search for packages (directories with __init__.py)
        if not found:
            found = [p.parent for p in Path.cwd().rglob(f"{top_level}/__init__.py")]
        if found:
            parent = str(found[0].parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            try:
                module = importlib.import_module(module_path)
            except ImportError as e2:
                # Fall through to file-path fallback below
                if not file_path:
                    raise ImportError(f"Cannot import module '{module_path}': {e2}") from e2
                module = _load_module_from_file(module_path, file_path)
        else:
            if file_path:
                module = _load_module_from_file(module_path, file_path)
            else:
                raise ImportError(
                    f"Cannot import module '{module_path}': not found on sys.path "
                    f"or under {Path.cwd()}"
                )

    # Walk the qualname (handles Class.method, nested classes, etc.)
    obj: Any = module
    for attr in qualname.split("."):
        obj = getattr(obj, attr, None)
        if obj is None:
            raise AttributeError(
                f"'{attr}' not found in '{module_path}' while resolving '{qualname}'"
            )

    if not callable(obj):
        raise TypeError(f"'{ref}' resolved to {type(obj).__name__}, not a callable")

    return obj


def _load_module_from_file(module_path: str, file_path: str) -> Any:
    """Load a module directly from a .py file — no __init__.py required."""
    abs_path = Path.cwd() / file_path
    if not abs_path.exists():
        raise ImportError(
            f"Cannot import module '{module_path}': stored file path "
            f"'{file_path}' not found at {abs_path}"
        )
    spec = importlib.util.spec_from_file_location(module_path, str(abs_path))
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Cannot import module '{module_path}': "
            f"spec_from_file_location failed for '{abs_path}'"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_path] = module
    spec.loader.exec_module(module)
    return module
