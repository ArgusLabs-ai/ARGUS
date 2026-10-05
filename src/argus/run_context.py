"""Bind persisted run ids to the test invocation that created them."""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token


class _RunCapture:
    """A per-test collection shared with worker threads in that test."""

    def __init__(self) -> None:
        self.run_ids: set[str] = set()
        self.token: Token[_RunCapture | None] | None = None
        self.closed = False


_current_capture: ContextVar[_RunCapture | None] = ContextVar("argus_run_capture", default=None)
_capture_lock = threading.Lock()
_active_captures: list[_RunCapture] = []


def begin_run_capture() -> _RunCapture:
    """Start collecting run ids for the current test invocation."""
    capture = _RunCapture()
    capture.token = _current_capture.set(capture)
    with _capture_lock:
        _active_captures.append(capture)
    return capture


def end_run_capture(capture: _RunCapture) -> None:
    """Stop collecting ids while retaining the ids already captured."""
    with _capture_lock:
        if capture.closed:
            return
        capture.closed = True
        try:
            _active_captures.remove(capture)
        except ValueError:
            pass
    if capture.token is not None and _current_capture.get() is capture:
        _current_capture.reset(capture.token)


def _active_capture() -> _RunCapture | None:
    with _capture_lock:
        for capture in reversed(_active_captures):
            if not capture.closed:
                return capture
    return None


def record_run_id(run_id: str) -> None:
    """Record a saved id for the current test, including from worker threads."""
    capture = _current_capture.get()
    if capture is None or capture.closed:
        capture = _active_capture()
    if capture is None:
        return
    with _capture_lock:
        if not capture.closed:
            capture.run_ids.add(run_id)


def captured_run_ids(capture: _RunCapture) -> set[str]:
    """Return a stable snapshot of ids captured by one test."""
    with _capture_lock:
        return set(capture.run_ids)
