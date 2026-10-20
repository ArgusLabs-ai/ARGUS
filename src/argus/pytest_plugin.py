"""Pytest plugin: ``pytest --argus`` fails tests whose ARGUS run was not clean.

Loaded via the ``pytest11`` entry point. Without ``--argus`` or ``--argus-report-only`` the
plugin is inert. Recording every LangGraph run during tests lives in
``argus.pytest_instrument``: a LangChain configure hook routes each graph run to
an ``ArgusRecorder`` bound to that graph. Nothing in LangGraph is patched (#78).

``--argus-report-only`` (or ``ARGUS_REPORT_ONLY=1``) records, grades and saves runs exactly as
``--argus`` does but never changes a test's outcome: a team workspace applies its own policy to
the runs a job uploads (``argus cloud upload``), so the job must not fail on a run that was not
clean. Either switch also lists the run ids the session saves (``.argus/session_runs.txt``) for
that upload.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from argus.check import evaluate_run
from argus.run_context import (
    begin_run_capture,
    begin_session,
    captured_run_ids,
    end_run_capture,
    end_session,
    report_only_from_env,
    set_report_only,
)
from argus.storage import load_run, session_path

_ITEM_RUN_CAPTURE = "_argus_run_capture"
_ARGUS_ENABLED = pytest.StashKey[bool]()
_REPORT_ONLY = pytest.StashKey[bool]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("argus")
    group.addoption(
        "--argus",
        action="store_true",
        default=False,
        help="Fail tests whose ARGUS-instrumented invoke was not clean "
        "(crash, silent failure, missing fields, semantic fail).",
    )
    group.addoption(
        "--argus-report-only",
        action="store_true",
        default=False,
        help="Record, grade and save ARGUS runs like --argus, but never fail or change a test "
        "because of one. For a job whose verdict is decided elsewhere (ARGUS_REPORT_ONLY=1 "
        "does the same).",
    )


def pytest_configure(config: pytest.Config) -> None:
    report_only = bool(config.getoption("--argus-report-only")) or report_only_from_env()
    if not (config.getoption("--argus") or report_only):
        return
    config.stash[_ARGUS_ENABLED] = True
    config.stash[_REPORT_ONLY] = report_only
    if report_only:
        set_report_only(True)
    # The process that starts the session empties the list; a pytest-xdist worker adds to it.
    begin_session(session_path(), fresh=not hasattr(config, "workerinput"))
    _maybe_install_auto_instrumentation()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Switch the recording hook off when the session ends.

    A `--argus` session run in-process (as `pytester` does) would otherwise keep
    recording every graph the host interpreter runs afterwards.
    """
    if not _argus_enabled(config):
        return
    end_session()
    set_report_only(False)
    try:
        from argus.pytest_instrument import uninstall_auto_instrumentation
    except ImportError:
        return
    uninstall_auto_instrumentation()


def _argus_enabled(config: pytest.Config) -> bool:
    return bool(config.stash.get(_ARGUS_ENABLED, False))


def _report_only(config: pytest.Config) -> bool:
    return bool(config.stash.get(_REPORT_ONLY, False))


def _maybe_install_auto_instrumentation() -> None:
    """Start recording LangGraph runs when the companion module is available."""
    try:
        from argus.pytest_instrument import install_auto_instrumentation
    except ImportError:
        return
    install_auto_instrumentation()


def pytest_runtest_setup(item: pytest.Item) -> None:
    if not _argus_enabled(item.config):
        return
    setattr(item, _ITEM_RUN_CAPTURE, begin_run_capture())


def _end_run_capture(item: pytest.Item) -> None:
    capture = getattr(item, _ITEM_RUN_CAPTURE, None)
    if capture is None:
        return
    end_run_capture(capture)
    delattr(item, _ITEM_RUN_CAPTURE)


def _fail_message(run_id: str, summary: str) -> str:
    return (
        f"argus check failed for run {run_id}: {summary}\n"
        "        pytest --argus fails tests whose instrumented invoke "
        "was not clean\n"
        f"        argus show {run_id}   |  argus check last"
    )


def _evaluate_new_runs(item: pytest.Item) -> str | None:
    capture = getattr(item, _ITEM_RUN_CAPTURE, None)
    if capture is None:
        return None
    for run_id in sorted(captured_run_ids(capture)):
        try:
            record = load_run(run_id)
        except (FileNotFoundError, ValueError):
            continue
        result = evaluate_run(record)
        if not result.passed:
            return _fail_message(run_id, result.summary)
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, Any, None]:
    outcome = yield
    if call.when != "call":
        return
    try:
        if not _argus_enabled(item.config) or _report_only(item.config):
            return
        report = outcome.get_result()
        if report.outcome != "passed":
            return
        message = _evaluate_new_runs(item)
        if message is None:
            return
        report.outcome = "failed"
        report.longrepr = message
    finally:
        _end_run_capture(item)


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Clean up if setup prevented the call report from being generated."""
    _end_run_capture(item)
