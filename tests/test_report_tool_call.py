"""B-14: the public report_tool_call seam files node-internal tool calls.

Register finding F-29 (second half): under LangGraph 1.x a node function can
hold an empty CallbackManager, so a tool invoked directly inside a node never
fires the recorder's on_tool_start and used to vanish from
``NodeEvent.tool_calls``. ``argus.report_tool_call`` files it manually, in the
same shape the callback path files, onto the step the call runs under.

No network, no real LLM: the tools are plain dicts / a trivial @tool.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

import argus
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.runnables.config import ensure_config  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    query: str
    out: str


def _build_app(node_fn) -> Any:
    g = StateGraph(_S)
    g.add_node("work", node_fn)
    g.add_edge(START, "work")
    return g.compile()


def _step(record, name="work"):
    steps = [s for s in record.steps if s.node_name == name]
    assert steps, f"no step named {name!r} in {[s.node_name for s in record.steps]}"
    return steps[0]


@pytest.mark.integration
def test_report_tool_call_files_into_step_end_to_end():
    """The F-29 pattern: node code files a direct-invoked tool call by hand."""

    def work(state: _S) -> dict:
        ok = argus.report_tool_call(
            "fake_tool",
            input={"q": state["query"]},
            output={"hits": ["doc-1"]},
        )
        assert ok is True
        return {"out": "done"}

    recorder = ArgusRecorder()
    recorder.attach(_build_app(work)).invoke({"query": "hello"})

    record = load_run(recorder.session.run_id)
    calls = _step(record).tool_calls
    assert len(calls) == 1
    assert calls[0]["name"] == "fake_tool"
    # Same shape as the callback path: on_tool_start files the string form.
    assert calls[0]["input"] == str({"q": "hello"})
    assert calls[0]["output"] == {"hits": ["doc-1"]}
    assert calls[0]["error"] is None


@pytest.mark.integration
def test_report_tool_call_outside_watched_run_warns_and_returns_false(caplog):
    """Outside a run: one loud warning on the argus logger, False, no raise."""
    with caplog.at_level("WARNING", logger="argus"):
        filed = argus.report_tool_call("fake_tool", input={"q": 1}, output="x")

    assert filed is False
    warnings = [r for r in caplog.records if r.name == "argus"]
    assert len(warnings) == 1
    assert "no active ArgusRecorder" in warnings[0].getMessage()
    assert "fake_tool" in warnings[0].getMessage()


@pytest.mark.integration
def test_report_tool_call_explicit_recorder_wins_over_registry(monkeypatch):
    """recorder= resolves even when the current-recorder registry is empty."""
    recorder = ArgusRecorder()
    # Simulate "no registry entry": the registry must not be consulted when an
    # explicit recorder is passed.
    monkeypatch.setattr("argus.recorder._active_recorders", {})

    def work(state: _S) -> dict:
        ok = argus.report_tool_call(
            "fake_tool",
            input={"q": "explicit"},
            output="from-explicit",
            recorder=recorder,
        )
        assert ok is True
        return {"out": "done"}

    recorder.attach(_build_app(work)).invoke({"query": "q"})

    # The registry was empty even mid-run: only the explicit param could file.

    calls = _step(load_run(recorder.session.run_id)).tool_calls
    assert [c["name"] for c in calls] == ["fake_tool"]
    assert calls[0]["output"] == "from-explicit"


@pytest.mark.integration
def test_report_tool_call_explicit_config_param():
    """config= wins for step resolution; a metadata-only dict resolves by node name."""

    def work(state: _S) -> dict:
        cfg = ensure_config({})
        ok = argus.report_tool_call(
            "fake_tool",
            input="cfg-path",
            output="ok",
            config=cfg,
        )
        assert ok is True
        return {"out": "done"}

    recorder = ArgusRecorder()
    recorder.attach(_build_app(work)).invoke({"query": "q"})

    calls = _step(load_run(recorder.session.run_id)).tool_calls
    assert len(calls) == 1
    assert calls[0]["input"] == "cfg-path"


@pytest.mark.integration
def test_report_tool_call_error_variant_lands_as_errored_call():
    """error= files an errored tool call; the graders blame the step for it."""

    def work(state: _S) -> dict:
        ok = argus.report_tool_call(
            "fake_tool",
            input={"q": "boom"},
            error=ValueError("kaboom"),
        )
        assert ok is True
        return {"out": "done"}

    recorder = ArgusRecorder()
    recorder.attach(_build_app(work)).invoke({"query": "q"})

    record = load_run(recorder.session.run_id)
    step = _step(record)
    assert len(step.tool_calls) == 1
    assert step.tool_calls[0]["error"] == "ValueError('kaboom')"
    assert step.tool_calls[0]["output"] is None
    # A swallowed tool error is the silent failure ARGUS exists for (#86).
    assert step.status == "fail"
    assert any(
        tf.failure_type == "tool_error" and tf.field_name == "fake_tool"
        for tf in step.inspection.tool_failures
    )


@pytest.mark.integration
def test_report_tool_call_dedups_against_live_callback_path():
    """invoke-then-report with the callback live: exactly one record survives."""

    @tool
    def fake_tool(query: str) -> str:
        """Fake tool, no network."""
        return f"result:{query}"

    def work(state: _S) -> dict:
        res = fake_tool.invoke({"query": "dedup"})
        ok = argus.report_tool_call(
            "fake_tool",
            input={"query": "dedup"},
            output=res,
        )
        assert ok is True
        return {"out": res}

    recorder = ArgusRecorder()
    recorder.attach(_build_app(work)).invoke({"query": "q"})

    calls = _step(load_run(recorder.session.run_id)).tool_calls
    assert len(calls) == 1, f"double-filed: {calls}"
    assert calls[0]["name"] == "fake_tool"
    assert calls[0]["output"] == "result:dedup"


@pytest.mark.integration
def test_report_tool_call_unresolvable_step_warns_and_returns_false(caplog):
    """A recorder with no matching open step: warning naming the reason, False."""

    def work(state: _S) -> dict:
        with caplog.at_level("WARNING", logger="argus"):
            filed = argus.report_tool_call(
                "fake_tool",
                config={"metadata": {"langgraph_node": "not_a_real_node"}},
            )
        assert filed is False
        return {"out": "done"}

    recorder = ArgusRecorder()
    recorder.attach(_build_app(work)).invoke({"query": "q"})

    warnings = [r for r in caplog.records if r.name == "argus"]
    assert len(warnings) == 1
    assert "no open node step" in warnings[0].getMessage()


@pytest.mark.integration
def test_report_tool_call_registry_cleared_after_run():
    """Run end clears the current-recorder registry (no stale recorder leaks)."""
    import argus.recorder as recorder_module

    def work(state: _S) -> dict:
        assert recorder_module._current_recorder() is not None
        return {"out": "done"}

    recorder = ArgusRecorder()
    recorder.attach(_build_app(work)).invoke({"query": "q"})

    assert recorder_module._current_recorder() is None
