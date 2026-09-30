"""Pytest plugin: ``pytest --argus`` fails tests whose ARGUS run was not clean.

Loaded via the ``pytest11`` entry point. Without ``--argus`` the plugin is
inert. Auto-wrapping LangGraph runtime methods during tests lives in
``argus.pytest_instrument`` (imported if present) so the CLI gate and the
auto-instrumentation can land on separate branches without duplicating the
plugin.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from argus.check import evaluate_run
from argus.run_context import (
    begin_run_capture,
    captured_run_ids,
    end_run_capture,
)
from argus.storage import load_run

_ITEM_RUN_CAPTURE = "_argus_run_capture"
_ARGUS_ENABLED = pytest.StashKey[bool]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("argus")
    group.addoption(
        "--argus",
        action="store_true",
        default=False,
        help="Fail tests whose ARGUS-instrumented invoke was not clean "
        "(crash, silent failure, missing fields, semantic fail).",
    )


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("--argus"):
        return
    config.stash[_ARGUS_ENABLED] = True
    _maybe_install_auto_instrumentation()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Undo the LangGraph patch when the session ends.

    Without this, a `--argus` session run in-process (as `pytester` does) leaves
    `Pregel.invoke` and friends patched in the host interpreter for good.
    """
    if not _argus_enabled(config):
        return
    try:
        from argus.pytest_instrument import uninstall_auto_instrumentation
    except ImportError:
        return
    uninstall_auto_instrumentation()


def _argus_enabled(config: pytest.Config) -> bool:
    return bool(config.stash.get(_ARGUS_ENABLED, False))


def _maybe_install_auto_instrumentation() -> None:
    """Install LangGraph auto-wrap when the companion module is available."""
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
        if not _argus_enabled(item.config):
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
