"""Bind persisted run ids to the test invocation that created them.

Two more pieces of per-session state live here because ``storage.save_run`` already imports
this module and the pytest plugin sets them up:

- the **session list**: every run id a ``pytest --argus`` / ``--argus-report-only`` session
  saves, one per line in ``.argus/session_runs.txt``. The per-test captures below are in memory
  and end with the test; ``argus cloud upload`` is another process and reads this file;
- **report-only mode**: runs are recorded and graded but never fail a test.
"""

from __future__ import annotations

import logging
import os
import threading
from contextvars import ContextVar, Token
from pathlib import Path

logger = logging.getLogger("argus")

SESSION_FILE = "session_runs.txt"
_TRUTHY = {"1", "true", "yes", "on"}


class _RunCapture:
    """A per-test collection shared with worker threads in that test."""

    def __init__(self) -> None:
        self.run_ids: set[str] = set()
        self.token: Token[_RunCapture | None] | None = None
        self.closed = False


_current_capture: ContextVar[_RunCapture | None] = ContextVar("argus_run_capture", default=None)
_capture_lock = threading.Lock()
_active_captures: list[_RunCapture] = []
_session_path: Path | None = None
_report_only = False


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
    """Record a saved id for the current test, including from worker threads.

    Also lists it in the session list, if a session began one, whether or not a test is
    capturing: a run saved from a fixture or at import still belongs to the job.
    """
    _list_in_session(run_id)
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


# ── the session list ─────────────────────────────────────────────────────────


def begin_session(path: Path, *, fresh: bool) -> None:
    """Start listing in ``path`` the ids this process saves.

    ``fresh`` empties the list first: the process that starts a session does that, so one job
    never uploads the runs an earlier job left in a shared workspace. A pytest-xdist worker
    passes ``fresh=False`` and adds to the controller's list. A list that cannot be made means
    no list (and a warning): saving a run must never fail because of it.
    """
    global _session_path  # noqa: PLW0603
    end_session()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            path.write_text("", encoding="utf-8")
    except OSError as exc:
        logger.warning("argus: no session list at %s (%s)", path, type(exc).__name__)
        return
    _session_path = path


def end_session() -> None:
    global _session_path  # noqa: PLW0603
    _session_path = None


def session_file() -> Path | None:
    """The list this process is adding to, or None."""
    return _session_path


def _list_in_session(run_id: str) -> None:
    path = _session_path
    if path is None:
        return
    try:
        # One short line per write on an append-mode file: processes do not interleave.
        with path.open("a", encoding="utf-8") as f:
            f.write(run_id + "\n")
    except OSError as exc:
        logger.warning("argus: run %s is not in the session list (%s)", run_id, type(exc).__name__)


def session_run_ids(path: Path) -> list[str] | None:
    """The run ids in a session list, in order and once each. None when there is no list.

    An empty list is a session that saved no runs; no list is a session that never ran.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return list(dict.fromkeys(line.strip() for line in lines if line.strip()))


# ── report-only mode ─────────────────────────────────────────────────────────


def report_only_from_env() -> bool:
    return os.environ.get("ARGUS_REPORT_ONLY", "").strip().lower() in _TRUTHY


def set_report_only(on: bool) -> None:
    global _report_only  # noqa: PLW0603
    _report_only = on


def report_only() -> bool:
    """Runs are recorded and graded but never fail a test: ``--argus-report-only`` or
    ``ARGUS_REPORT_ONLY=1``."""
    return _report_only or report_only_from_env()
