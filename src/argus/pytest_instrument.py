"""Record every LangGraph run during ``pytest --argus`` — without patching anything (#78).

LangChain's public ``register_configure_hook`` adds one handler to every runnable
invoked in the process. That handler, :class:`_AutoRecorder`, watches for a graph
starting. It runs inline (``run_inline``), so at that moment the compiled graph is
on the call stack inside ``Pregel.stream`` / ``astream``; it binds an
:class:`~argus.recorder.ArgusRecorder` to that graph exactly as ``attach()`` would
(same topology, reducers, subgraphs) and forwards the run's callbacks to it.
Nothing on ``StateGraph`` or ``Pregel`` is replaced. The old version patched
``StateGraph.compile`` and six ``Pregel`` methods; an install → uninstall →
install cycle once left LangGraph patched in the host interpreter for good.

A graph the test already attached (``ArgusRecorder().attach(app)`` tags its runs;
``ArgusWatcher`` marks the compiled app) is left to that recorder, so no run is
recorded twice. LangChain offers no way to unregister a hook, so
``uninstall_auto_instrumentation`` switches the handler off instead.
"""

from __future__ import annotations

import sys
import threading
from contextvars import ContextVar
from types import FrameType
from typing import Any
from uuid import UUID

try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.tracers.context import register_configure_hook

    _HAS_LANGCHAIN = True
except ImportError:  # pragma: no cover - langchain-core is a recorder dependency
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _HAS_LANGCHAIN = False

from argus.recorder import ATTACHED_MARK

__all__ = ["install_auto_instrumentation", "uninstall_auto_instrumentation"]

_handler: _AutoRecorder | None = None


def install_auto_instrumentation() -> None:
    """Record every LangGraph run from here on. Idempotent."""
    global _handler
    if not _HAS_LANGCHAIN:
        return
    if _handler is None:
        _handler = _AutoRecorder()
        # The default (not a value set in this context) makes the handler reach
        # worker threads a test starts itself, which do not inherit contextvars.
        register_configure_hook(ContextVar("argus_pytest", default=_handler), inheritable=True)
    _handler.active = True


def uninstall_auto_instrumentation() -> None:
    """Stop recording. The hook stays registered but does nothing."""
    if _handler is not None:
        _handler.active = False


def _graph_on_stack() -> Any:
    """The innermost compiled LangGraph whose ``stream`` / ``astream`` is running."""
    try:
        from langgraph.pregel import Pregel
    except ImportError:  # pragma: no cover
        return None
    frame: FrameType | None = sys._getframe(2)
    while frame is not None:
        owner = frame.f_locals.get("self")
        if isinstance(owner, Pregel) and frame.f_code.co_name in ("stream", "astream"):
            return owner
        frame = frame.f_back
    return None


class _AutoRecorder(BaseCallbackHandler):
    """Routes each graph run's callbacks to a recorder bound to that graph."""

    run_inline = True  # the compiled graph is only on the stack if we run in its frame
    raise_error = True  # a recorder bug should surface, as with an explicit attach

    def __init__(self) -> None:
        self.active = False
        self._lock = threading.Lock()
        self._route: dict[UUID, Any] = {}  # run id -> ArgusRecorder
        self._root: dict[UUID, UUID] = {}  # run id -> graph run that owns it
        self._recorders: dict[int, tuple[Any, Any]] = {}  # id(app) -> (app, recorder)

    # ── routing ──────────────────────────────────────────────────────────────

    def _recorder_for(self, app: Any) -> Any:
        from argus.recorder import ArgusRecorder

        with self._lock:
            hit = self._recorders.get(id(app))
            if hit is not None and hit[0] is app:
                return hit[1]
        rec = ArgusRecorder(semantic_judge=False)
        rec.bind(app)
        with self._lock:
            self._recorders[id(app)] = (app, rec)
        return rec

    def _start(self, run_id: UUID, parent_run_id: UUID | None, metadata: Any) -> Any:
        """Who records this run, deciding on a graph's own start."""
        with self._lock:
            rec = self._route.get(parent_run_id) if parent_run_id is not None else None
            if rec is not None:
                self._route[run_id] = rec
                self._root[run_id] = self._root[parent_run_id]  # type: ignore[index]
                return rec
        if (metadata or {}).get(ATTACHED_MARK) or (metadata or {}).get("langgraph_node"):
            return None  # an explicit attach records it; or a node of an unrecorded graph
        app = _graph_on_stack()
        if app is None or getattr(app, "_argus_auto_persist", False):
            return None  # not a graph, or ArgusWatcher owns it
        rec = self._recorder_for(app)
        with self._lock:
            self._route[run_id] = rec
            self._root[run_id] = run_id
        return rec

    def _of(self, run_id: UUID, parent_run_id: UUID | None) -> Any:
        with self._lock:
            return self._route.get(run_id) or (
                self._route.get(parent_run_id) if parent_run_id is not None else None
            )

    def _forget(self, run_id: UUID) -> None:
        with self._lock:
            if self._root.get(run_id) != run_id:
                return
            for rid in [r for r, root in self._root.items() if root == run_id]:
                self._route.pop(rid, None)
                self._root.pop(rid, None)

    # ── callbacks ────────────────────────────────────────────────────────────

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> None:
        if not self.active:
            return
        rec = self._start(run_id, parent_run_id, metadata)
        if rec is not None:
            rec.on_chain_start(
                serialized,
                inputs,
                run_id=run_id,
                parent_run_id=parent_run_id,
                metadata=metadata,
                **kwargs,
            )

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        rec = self._of(run_id, kwargs.get("parent_run_id"))
        if rec is not None:
            rec.on_chain_end(outputs, run_id=run_id, **kwargs)
            self._forget(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        rec = self._of(run_id, kwargs.get("parent_run_id"))
        if rec is not None:
            rec.on_chain_error(error, run_id=run_id, **kwargs)
            self._forget(run_id)

    def on_tool_start(
        self,
        serialized: Any,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        rec = self._of(run_id, parent_run_id)
        if rec is not None:
            with self._lock:
                self._route[run_id] = rec
                self._root[run_id] = self._root.get(parent_run_id, run_id)  # type: ignore[arg-type]
            rec.on_tool_start(
                serialized, input_str, run_id=run_id, parent_run_id=parent_run_id, **kwargs
            )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        rec = self._of(run_id, kwargs.get("parent_run_id"))
        if rec is not None:
            rec.on_tool_end(output, run_id=run_id, **kwargs)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        rec = self._of(run_id, kwargs.get("parent_run_id"))
        if rec is not None:
            rec.on_tool_error(error, run_id=run_id, **kwargs)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        rec = self._of(run_id, kwargs.get("parent_run_id"))
        if rec is not None:
            rec.on_llm_end(response, run_id=run_id, **kwargs)
