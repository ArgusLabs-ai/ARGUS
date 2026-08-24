from __future__ import annotations

import functools
import inspect
import os
import threading
from typing import Any, Callable

from argus.checkpoints import mark_checkpoint_resumed
from argus.models import ArgusConfig
from argus.patcher import extract_conditional_sources, extract_edge_map, extract_fn, patch_graph
from argus.session import ArgusSession

# Backward-compat alias — RunSession was the internal name before ArgusSession was public
RunSession = ArgusSession

_RUNTIME_METHODS = ("invoke", "ainvoke", "stream", "astream", "batch", "abatch")


def _is_compiled_graph(obj: Any) -> bool:
    """True for a LangGraph compiled app, False for a StateGraph builder."""
    if not callable(getattr(obj, "invoke", None)):
        return False
    # Builders have compile() + add_node(); compiled apps have invoke() + builder.
    if hasattr(obj, "add_node") and hasattr(obj, "compile"):
        return False
    return True


class ArgusWatcher:
    """LangGraph adapter for ArgusSession.

    One public API — ``attach()`` works for both uncompiled StateGraphs and
    already-compiled apps. Runs persist when the outermost graph call
    (``invoke``, ``batch``, ``stream``, …) returns; ``finalize()`` is
    optional and idempotent.

        watcher = ArgusWatcher()
        app = watcher.attach(graph)     # StateGraph or compiled app
        result = app.invoke(state)      # persisted automatically
        print(watcher.run_id)

    Constructor still accepts an uncompiled graph (patches it; compile yourself):

        watcher = ArgusWatcher(graph)
        app = graph.compile()
        result = app.invoke(state)

    ``watch()`` / ``watch_compiled()`` remain as thin wrappers around attach().

    Usage (semantic validators):
        watcher = ArgusWatcher(graph, validators={
            "summarize": lambda out: (len(out.get("summary","")) > 10, "Summary too short"),
            "*": lambda out: ("error" not in out, f"Error: {out.get('error')}"),
        })

    Usage (opt in to LLM semantic judge — default is off / heuristics-only):
        watcher = ArgusWatcher(graph, semantic_judge=True)    # after argus key set

    Usage (framework-agnostic, without LangGraph):
        from argus import ArgusSession   # use ArgusSession directly
    """

    def __init__(
        self,
        graph: Any = None,
        *,
        config: ArgusConfig | None = None,
        max_field_size: int = 50_000,
        validators: dict[str, Callable[[dict], tuple[bool, str]]] | None = None,
        strict: bool = False,
        investigate: bool | str = True,
        redact_keys: set[str] | list[str] | None = None,
        redact_functions: dict[str, Callable[[Any], Any]] | None = None,
        redact_patterns: bool = False,
        persist_state: bool = True,
        record_http: bool = True,
        semantic_judge: bool = False,
        judge_model: str = "gpt-4o",
    ) -> None:
        # Build config from typed object or loose kwargs (backward compat)
        if config is not None:
            self._config = config
        else:
            self._config = ArgusConfig(
                max_field_size=max_field_size,
                strict=strict,
                investigate=investigate,
                redact_keys=redact_keys,
                redact_functions=redact_functions,
                redact_patterns=redact_patterns,
                persist_state=persist_state,
                record_http=record_http,
                semantic_judge=semantic_judge,
                judge_model=judge_model,
            )
        self._redact_functions = (config.redact_functions if config else redact_functions) or {}
        self._validators = validators or {}
        self._http_recorder_ctx = None
        self._http_recorder = None
        self._session: ArgusSession | None = None
        self._persist_depth = 0
        self._persist_lock = threading.Lock()

        # If graph was passed to constructor, instrument it. Compiled apps
        # go through attach() (aliases invoke onto the original object).
        # Uncompiled builders are patched in place so the caller can still
        # compile() themselves — matching the historical constructor contract.
        if graph is not None:
            if _is_compiled_graph(graph):
                self.attach(graph)
            else:
                self._patch_builder(graph)
                self._wrap_compile(graph)

    def __enter__(self) -> ArgusWatcher:
        return self

    def __exit__(self, *args: Any) -> None:
        self.finalize()

    def __del__(self) -> None:
        if self._session is not None and self._session._is_cyclic and not self._session._completed:
            import warnings

            warnings.warn(
                f"ArgusWatcher for cyclic graph was garbage-collected before invoke() "
                f"returned. Runs persist when the outermost invoke() returns "
                f"(finalize() is optional). Run ID: {self._session.run_id}",
                stacklevel=1,
            )

    @property
    def run_id(self) -> str | None:
        """The run ID for the current session, or None if attach() hasn't been called."""
        if self._session is None:
            return None
        return self._session.run_id

    def attach(self, graph: Any) -> Any:
        """Attach ARGUS to a StateGraph or an already-compiled graph.

        One call covers both cases — no need to know whether the graph has
        been compiled yet. Always returns a compiled app with monitoring.
        The run is persisted when the outermost ``invoke`` / ``ainvoke`` /
        ``stream`` / ``astream`` / ``batch`` / ``abatch`` call returns;
        ``finalize()`` is optional.

        Args:
            graph: A LangGraph ``StateGraph`` or a compiled app
                (``graph.compile()`` result).

        Returns:
            A compiled graph with ARGUS monitoring. For a compiled input,
            runtime methods are also aliased onto the original object so
            ``app.invoke()`` keeps working without reassignment.

        Usage::

            watcher = ArgusWatcher()
            app = watcher.attach(graph)          # uncompiled StateGraph
            app = watcher.attach(compiled_app)   # already compiled
            app.invoke(state)                    # persisted automatically
        """
        if _is_compiled_graph(graph):
            return self._attach_compiled(graph)

        if not hasattr(graph, "nodes"):
            raise ValueError(
                "argus.attach() expects a LangGraph StateGraph or a compiled graph. "
                "Pass the StateGraph before compile(), or the app returned by compile()."
            )

        self._patch_builder(graph)
        self._wrap_compile(graph)
        return graph.compile()

    def watch_compiled(self, compiled_graph: Any) -> Any:
        """Attach ARGUS to an already-compiled LangGraph graph.

        Thin wrapper around ``attach()``. Prefer ``attach()`` for new code.
        """
        return self.attach(compiled_graph)

    def watch(self, graph: Any) -> None:
        """Attach ARGUS to a LangGraph StateGraph. Must be called before compile().

        Thin wrapper around ``attach()``. Prefer ``attach()`` for new code —
        it also accepts already-compiled graphs.
        """
        if _is_compiled_graph(graph):
            raise ValueError(
                "watch() expects an uncompiled StateGraph. "
                "Use attach() for compiled graphs: app = watcher.attach(app)."
            )
        self.attach(graph)

    def _attach_compiled(self, compiled_graph: Any) -> Any:
        """Patch the underlying builder, recompile, and auto-persist on invoke."""
        builder = getattr(compiled_graph, "builder", None)
        if builder is None:
            raise ValueError(
                "Cannot extract StateGraph from this compiled graph. "
                "Your LangGraph version may be too old for attach(). "
                "Either upgrade langgraph or pass the StateGraph before compile()."
            )

        compile_kwargs: dict[str, Any] = {}
        for attr in ("checkpointer", "interrupt_before", "interrupt_after"):
            val = getattr(compiled_graph, attr, None)
            if val is not None:
                compile_kwargs[attr] = val

        self._patch_builder(builder)
        self._wrap_compile(builder)
        monitored = builder.compile(**compile_kwargs)
        self._alias_runtime(compiled_graph, monitored)
        return monitored

    def _patch_builder(self, graph: Any) -> None:
        """Instrument a StateGraph in place (session, patch, HTTP recorder)."""
        if not hasattr(graph, "nodes"):
            raise ValueError(
                "argus.attach() expects a LangGraph StateGraph instance with a .nodes attribute."
            )

        node_names = list(graph.nodes.keys())
        edge_map = extract_edge_map(graph)

        # Load .env early so credentials are available for auto-detection
        try:
            from dotenv import load_dotenv

            load_dotenv(override=True)
        except ImportError:
            pass

        # Auto-enable LLM investigation if key is available or user is logged in
        from argus.llm_proxy import is_available as _llm_available

        cfg = self._config
        llm_inv_config = None
        if (cfg.investigate or cfg.semantic_judge) and _llm_available():
            from argus.models import LLMInvestigationConfig

            always = (cfg.investigate == "always") or cfg.semantic_judge
            llm_inv_config = LLMInvestigationConfig(
                enabled=True,
                always_investigate=always,
                model=cfg.judge_model,
                semantic_check=cfg.semantic_judge,
            )

        self._session = ArgusSession(
            config=cfg,
            max_field_size=cfg.max_field_size,
            validators=self._validators,
            strict=cfg.strict,
            llm_investigation=llm_inv_config,
            redact_keys=cfg.redact_keys,
            redact_functions=self._redact_functions,
            redact_patterns=cfg.redact_patterns,
            persist_state=cfg.persist_state,
        )
        self._session.set_node_names(node_names)
        self._session.set_edges(edge_map)
        self._session.set_conditional_sources(extract_conditional_sources(graph))

        if self._session._is_cyclic:
            import logging

            logging.getLogger("argus").info(
                "Cyclic graph detected — run will persist when invoke() returns "
                "(finalize() is optional)."
            )

        # Extract reducer functions from graph schema (Annotated hints + channels)
        from argus.utils.type_introspection import extract_reducer_fields

        self._session.reducer_fields = extract_reducer_fields(graph)

        # store references to original node functions for inspector
        self._session.node_fn_registry = {
            name: extract_fn(graph.nodes[name]) for name in node_names
        }

        # Auto-capture each node function's import path for factory-free replay
        self._session.node_fn_refs = _capture_node_fn_refs(self._session.node_fn_registry)
        self._session.node_fn_paths = _capture_node_fn_paths(self._session.node_fn_registry)

        # Auto-capture the caller's module:function as fallback
        self._session.app_factory_ref = _detect_caller_factory()

        patch_graph(graph, self._session)

        # Start HTTP recording if requested
        if cfg.record_http:
            try:
                from argus.http_recorder import record_http

                self._http_recorder_ctx = record_http()
                self._http_recorder = self._http_recorder_ctx.__enter__()
            except Exception:
                pass  # HTTP recording is best-effort

    def _wrap_compile(self, graph: Any) -> None:
        """Make graph.compile() return an app that auto-persists on invoke()."""
        if getattr(graph, "_argus_compile_wrapped", False):
            return
        original_compile = graph.compile

        @functools.wraps(original_compile)
        def _compile(*args: Any, **kwargs: Any) -> Any:
            compiled = original_compile(*args, **kwargs)
            return self._install_auto_persist(compiled)

        graph.compile = _compile  # type: ignore[method-assign]
        graph._argus_compile_wrapped = True

    def _install_auto_persist(self, compiled: Any) -> Any:
        """Wrap runtime methods so the run is saved when the outermost call finishes.

        LangGraph ``batch()`` / ``abatch()`` call ``invoke()`` / ``ainvoke()``
        once per item. A shared depth counter finalizes only when the
        outermost user-facing call returns, so later items are not dropped.
        """
        if getattr(compiled, "_argus_auto_persist", False):
            return compiled
        self._wrap_runtime_call(compiled, "invoke", is_async=False)
        self._wrap_runtime_call(compiled, "ainvoke", is_async=True)
        self._wrap_runtime_gen(compiled, "stream", is_async=False)
        self._wrap_runtime_gen(compiled, "astream", is_async=True)
        self._wrap_runtime_call(compiled, "batch", is_async=False)
        self._wrap_runtime_call(compiled, "abatch", is_async=True)
        compiled._argus_auto_persist = True
        return compiled

    def _enter_persist_scope(self) -> None:
        with self._persist_lock:
            self._persist_depth += 1
            if self._session is not None:
                # Outermost call on an already-persisted session: start a fresh
                # run so each invoke()/stream()/batch() writes its own record
                # (not just the first one).
                if self._persist_depth == 1 and self._session._completed:
                    self._session.begin_new_run()
                    self._rearm_http_recorder()
                # Nested invoke (batch item, stream internals): don't mark the
                # session complete until the outermost call returns.
                self._session._defer_auto_finalize = self._persist_depth > 1

    def _rearm_http_recorder(self) -> None:
        """Restart HTTP recording for a new run (finalize() tore down the last)."""
        if not self._config.record_http or self._http_recorder is not None:
            return
        try:
            from argus.http_recorder import record_http

            self._http_recorder_ctx = record_http()
            self._http_recorder = self._http_recorder_ctx.__enter__()
        except Exception:
            pass  # HTTP recording is best-effort

    def _exit_persist_scope(self) -> None:
        should_persist = False
        with self._persist_lock:
            self._persist_depth = max(0, self._persist_depth - 1)
            if self._session is not None:
                self._session._defer_auto_finalize = self._persist_depth > 1
            should_persist = self._persist_depth == 0
        if should_persist:
            self.finalize()

    def _wrap_runtime_call(self, compiled: Any, name: str, *, is_async: bool) -> None:
        original = getattr(compiled, name, None)
        if not callable(original):
            return
        if is_async:

            @functools.wraps(original)
            async def _async_call(*args: Any, **kwargs: Any) -> Any:
                self._enter_persist_scope()
                try:
                    return await original(*args, **kwargs)
                finally:
                    self._exit_persist_scope()

            setattr(compiled, name, _async_call)
            return

        @functools.wraps(original)
        def _sync_call(*args: Any, **kwargs: Any) -> Any:
            self._enter_persist_scope()
            try:
                return original(*args, **kwargs)
            finally:
                self._exit_persist_scope()

        setattr(compiled, name, _sync_call)

    def _wrap_runtime_gen(self, compiled: Any, name: str, *, is_async: bool) -> None:
        original = getattr(compiled, name, None)
        if not callable(original):
            return
        if is_async:

            @functools.wraps(original)
            async def _async_gen(*args: Any, **kwargs: Any) -> Any:
                self._enter_persist_scope()
                try:
                    agen = original(*args, **kwargs)
                    if inspect.isawaitable(agen):
                        agen = await agen
                    async for item in agen:
                        yield item
                finally:
                    self._exit_persist_scope()

            setattr(compiled, name, _async_gen)
            return

        @functools.wraps(original)
        def _sync_gen(*args: Any, **kwargs: Any) -> Any:
            iterator = original(*args, **kwargs)
            if hasattr(iterator, "__iter__") and not isinstance(
                iterator, (dict, list, tuple, str, bytes)
            ):

                def _gen() -> Any:
                    self._enter_persist_scope()
                    try:
                        yield from iterator
                    finally:
                        self._exit_persist_scope()

                return _gen()
            self._enter_persist_scope()
            try:
                return iterator
            finally:
                self._exit_persist_scope()

        setattr(compiled, name, _sync_gen)

    def _alias_runtime(self, dest: Any, src: Any) -> None:
        """Copy src runtime methods onto dest so the original compiled object stays live."""
        if dest is src:
            return
        for name in _RUNTIME_METHODS:
            if hasattr(src, name):
                try:
                    setattr(dest, name, getattr(src, name))
                except Exception:
                    pass

    def finalize(self) -> None:
        """Persist the run record.

        Optional — ``attach()`` persists when the outermost graph call
        returns, including cyclic graphs. Safe to call explicitly (tests,
        early flush); a second call is a no-op.
        """
        if self._session is not None:
            self._session.finalize()

            # Save HTTP recordings if any (skip in dry-run mode — VAR-75)
            if self._http_recorder is not None and not self._session._dry_run:
                try:
                    interactions = self._http_recorder.interactions
                    if interactions:
                        from argus.http_recorder import save_http_interactions

                        save_http_interactions(self._session.run_id, interactions)
                except Exception:
                    pass  # best-effort

            # Clean up HTTP recorder context
            if self._http_recorder_ctx is not None:
                try:
                    self._http_recorder_ctx.__exit__(None, None, None)
                except Exception:
                    pass
                self._http_recorder_ctx = None
                self._http_recorder = None

    def resume(self, checkpoint_run_id: str, app: Any, resume_input: Any = None) -> None:
        """Resume a previously interrupted (human-approval) run.

        Marks the checkpoint as resumed, invokes the app, then finalizes.

        Args:
            checkpoint_run_id: run_id of the interrupted run
            app: the compiled LangGraph app (same instance used for the original run)
            resume_input: input to pass to app.invoke() — often None for LangGraph resumes
        """
        mark_checkpoint_resumed(checkpoint_run_id)
        if self._session is not None:
            self._session.reset_for_resume(checkpoint_run_id)
        app.invoke(resume_input)
        self.finalize()


def _detect_caller_factory() -> str | None:
    """Walk the call stack to find the user function that called watch().

    Uses the frame's __name__ (the real Python module name) so the result
    works regardless of cwd or project layout — pip-installed packages,
    src/ layouts, nested folders all resolve correctly.

    Skips functions that appear to compile AND invoke the graph (they would
    return a result dict, not a StateGraph).

    Returns "module:function" string suitable for importlib, or None.
    """
    fallback: str | None = None

    for frame_info in inspect.stack()[2:]:  # skip _detect_caller_factory + watch
        func_name = frame_info.function
        filename = frame_info.filename

        # Skip argus internals, stdlib, site-packages
        if "site-packages" in filename or "argus/" in filename:
            continue
        # Must be inside a named function (not module-level <module>)
        if func_name == "<module>":
            continue

        # Use the real Python module name from the frame globals
        module_name = frame_info.frame.f_globals.get("__name__")
        if not module_name or module_name == "__main__":
            # __main__ isn't importable — fall back to file-path heuristic
            module_name = _module_from_filepath(filename)
            if not module_name:
                continue

        ref = f"{module_name}:{func_name}"

        # Check if this frame has a compiled graph in its locals — if so,
        # the function likely compiles AND invokes the graph and would
        # return a result dict, not the StateGraph builder.
        locals_dict = frame_info.frame.f_locals
        has_compiled = any(
            hasattr(v, "invoke") and hasattr(v, "get_graph")
            for v in locals_dict.values()
            if v is not None
        )
        if has_compiled:
            # Remember as fallback but keep looking for a better candidate
            if fallback is None:
                fallback = ref
            continue

        return ref

    return fallback


def _capture_node_fn_refs(
    fn_registry: dict[str, Any],
) -> dict[str, str]:
    """Build a {node_name: "module:qualname"} map from the live function registry.

    At replay time, each function can be re-imported with importlib — no factory needed.
    """
    refs: dict[str, str] = {}
    for name, fn in fn_registry.items():
        module = getattr(fn, "__module__", None)
        qualname = getattr(fn, "__qualname__", None)
        if not module or not qualname:
            continue
        if module == "__main__":
            # __main__ isn't importable — try to resolve from source file
            src_file = getattr(fn, "__code__", None)
            if src_file:
                resolved = _module_from_filepath(src_file.co_filename)
                if resolved:
                    module = resolved
                else:
                    continue
            else:
                continue
        refs[name] = f"{module}:{qualname}"
    return refs


def _capture_node_fn_paths(
    fn_registry: dict[str, Any],
) -> dict[str, str]:
    """Build a {node_name: relative_file_path:line} map from live functions.

    Stores the source file path (with line number) relative to cwd so replay
    can fall back to direct file loading when importlib fails (e.g. no
    __init__.py).  Format: ``"path/to/file.py:42"``.
    """
    paths: dict[str, str] = {}
    for name, fn in fn_registry.items():
        code = getattr(fn, "__code__", None)
        if code and code.co_filename:
            try:
                rel = os.path.relpath(code.co_filename, os.getcwd())
            except ValueError:
                # On Windows, relpath fails across drives
                rel = code.co_filename
            line = code.co_firstlineno
            paths[name] = f"{rel}:{line}"
    return paths


def _module_from_filepath(filepath: str) -> str | None:
    """Convert an absolute .py path to a dotted module name relative to cwd.

    Fallback used when the frame's __name__ is '__main__'.
    """
    cwd = os.getcwd()
    try:
        rel = os.path.relpath(filepath, cwd)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel.removesuffix(".py").replace(os.sep, ".")
