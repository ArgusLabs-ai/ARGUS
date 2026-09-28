"""S8: on Python < 3.11 an async node's tool calls are invisible unless the node
forwards `config`. ARGUS cannot fix that without patching the graph, so attach
says so once instead of grading the run on a trace it knows may be missing I/O."""

from __future__ import annotations

import logging
from typing import TypedDict

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402

import argus.recorder as recorder_mod  # noqa: E402
from argus.recorder import ArgusRecorder  # noqa: E402


class _S(TypedDict, total=False):
    a: int


def _app(async_node: bool):
    async def anode(s):
        return {"a": 1}

    def snode(s):
        return {"a": 1}

    g = StateGraph(_S)
    g.add_node("n", anode if async_node else snode)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    return g.compile()


@pytest.mark.parametrize(
    "old_python,async_node,warned",
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_attach_warns_about_async_nodes_on_old_python(monkeypatch, caplog, old_python, async_node, warned):
    monkeypatch.setattr(recorder_mod, "_CONTEXT_PROPAGATES", not old_python)
    with caplog.at_level(logging.WARNING, logger="argus"):
        ArgusRecorder(semantic_judge=False).attach(_app(async_node))
    hits = [r for r in caplog.records if "async" in r.getMessage() and "3.11" in r.getMessage()]
    assert bool(hits) is warned, [r.getMessage() for r in caplog.records]
