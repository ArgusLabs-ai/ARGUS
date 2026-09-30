"""Auto-instrument LangGraph graphs during ``pytest --argus``.

Patches ``StateGraph.compile`` and all Pregel runtime methods matching
``ArgusWatcher`` (``invoke`` / ``ainvoke`` / ``stream`` / ``astream`` /
``batch`` / ``abatch``) so test code that starts a graph without an explicit
``ArgusWatcher.attach()`` is still watched. Idempotent; call
``uninstall_auto_instrumentation()`` to restore originals (used by tests).
"""

from __future__ import annotations

import functools
import inspect
import threading
from typing import Any

from argus.watcher import _RUNTIME_METHODS

_tls = threading.local()
_installed = False
_originals: dict[str, Any] = {}


def install_auto_instrumentation() -> None:
    """Patch LangGraph compile and runtime methods for pytest inspection."""
    global _installed
    if _installed:
        return
    try:
        from langgraph.graph.state import StateGraph
    except ImportError:
        return

    _wrap_compile(StateGraph)
    _wrap_pregel_runtime_methods()
    _installed = True


def uninstall_auto_instrumentation() -> None:
    """Restore LangGraph methods patched by ``install_auto_instrumentation``."""
    global _installed
    if not _installed:
        return
    try:
        from langgraph.graph.state import StateGraph

        original = _originals.get("compile")
        if original is not None:
            StateGraph.compile = original  # type: ignore[method-assign]
    except ImportError:
        pass
    try:
        from langgraph.pregel import Pregel

        for name in _RUNTIME_METHODS:
            original = _originals.get(name)
            if original is not None:
                setattr(Pregel, name, original)
    except ImportError:
        pass
    _originals.clear()
    _installed = False


def _in_attach() -> bool:
    return bool(getattr(_tls, "in_attach", False))


def _attach(compiled: Any) -> Any:
    from argus import ArgusWatcher

    _tls.in_attach = True
    try:
        watcher = ArgusWatcher(
            semantic_judge=False,
            investigate=False,
            record_http=False,
        )
        return watcher.attach(compiled)
    finally:
        _tls.in_attach = False


def _wrap_compile(state_graph_cls: Any) -> None:
    original: Any = state_graph_cls.compile
    if getattr(original, "_argus_pytest_wrapped", False):
        return
    _originals["compile"] = original

    @functools.wraps(original)
    def compile(self: Any, *args: Any, **kwargs: Any) -> Any:
        compiled = original(self, *args, **kwargs)
        if _in_attach():
            return compiled
        # ArgusWatcher already owns this builder (instance compile wrapper).
        if getattr(self, "_argus_compile_wrapped", False):
            return compiled
        if getattr(compiled, "_argus_auto_persist", False):
            return compiled
        return _attach(compiled)

    compile._argus_pytest_wrapped = True  # type: ignore[attr-defined]
    state_graph_cls.compile = compile


def _should_skip_attach(self: Any) -> bool:
    return _in_attach() or getattr(self, "_argus_auto_persist", False)


def _wrap_pregel_runtime_methods() -> None:
    """Patch Pregel runtime entry points to match ``ArgusWatcher._RUNTIME_METHODS``."""
    try:
        from langgraph.pregel import Pregel
    except ImportError:
        return

    for name in _RUNTIME_METHODS:
        _wrap_pregel_method(Pregel, name)


def _wrap_pregel_method(pregel_cls: Any, name: str) -> None:
    original = getattr(pregel_cls, name, None)
    if not callable(original):
        return
    if getattr(original, "_argus_pytest_wrapped", False):
        return
    _originals[name] = original

    if name in ("ainvoke", "abatch"):
        wrapper = _make_async_call_wrapper(original, name)
    elif name == "astream":
        wrapper = _make_async_gen_wrapper(original, name)
    else:
        # invoke, stream, batch — sync call or sync iterator return
        wrapper = _make_sync_call_wrapper(original, name)

    wrapper._argus_pytest_wrapped = True  # type: ignore[attr-defined]
    setattr(pregel_cls, name, wrapper)


def _make_sync_call_wrapper(original: Any, name: str) -> Any:
    @functools.wraps(original)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _should_skip_attach(self):
            return original(self, *args, **kwargs)
        app = _attach(self)
        return getattr(app, name)(*args, **kwargs)

    return wrapper


def _make_async_call_wrapper(original: Any, name: str) -> Any:
    @functools.wraps(original)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _should_skip_attach(self):
            return await original(self, *args, **kwargs)
        app = _attach(self)
        return await getattr(app, name)(*args, **kwargs)

    return wrapper


def _make_async_gen_wrapper(original: Any, name: str) -> Any:
    @functools.wraps(original)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _should_skip_attach(self):
            agen = original(self, *args, **kwargs)
            if inspect.isawaitable(agen):
                agen = await agen
            async for item in agen:
                yield item
            return
        app = _attach(self)
        agen = getattr(app, name)(*args, **kwargs)
        if inspect.isawaitable(agen):
            agen = await agen
        async for item in agen:
            yield item

    return wrapper
