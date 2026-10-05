"""E4b / #131: a loop that appends to a list must not hide a failed iteration.

``_apply_loop_retries`` relabels earlier iterations ``retried`` once the last
round passes, and the gate skips ``retried``. Right when the field is
overwritten (writer/critic ``draft``) or is message history (``add_messages``,
so a ReAct agent that 404s and retries stays clean). Wrong for a data
accumulator: page 1 timing out and returning ``[]`` is still in ``rows``
after pages 2 and 3 succeed, and used to grade clean.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

import pytest

from argus.check import evaluate_run
from argus.ledger import reducer_kinds
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _grade(app, payload):
    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(app).invoke(payload)
    record = load_run(recorder.session.run_id)
    return evaluate_run(record), record


def test_reducer_kinds_distinguish_add_from_add_messages():
    kinds = reducer_kinds({"rows": operator.add, "messages": add_messages})
    assert kinds["rows"] == "add"
    assert kinds["messages"] == "add_messages"


class _Pages(TypedDict, total=False):
    rows: Annotated[list, operator.add]
    cursor: int


def _pagination_app(fail_page: int | None):
    @tool
    def fetch_page(page: int) -> dict:
        """Fetch one page of rows."""
        if page == fail_page:
            raise TimeoutError(f"page {page} timed out")
        return {"items": [{"id": page}]}

    def fetch(state):
        page = state.get("cursor", 0) + 1
        try:
            items = fetch_page.invoke({"page": page})["items"]
        except Exception:
            items = []  # swallowed: the page is missing from the accumulator
        return {"rows": items, "cursor": page}

    def route(state):
        return "fetch" if state.get("cursor", 0) < 3 else "done"

    def done(state):
        return {"cursor": state.get("cursor", 0)}

    g = StateGraph(_Pages)
    g.add_node("fetch", fetch)
    g.add_node("done", done)
    g.add_edge(START, "fetch")
    g.add_conditional_edges("fetch", route, {"fetch": "fetch", "done": "done"})
    g.add_edge("done", END)
    return g.compile()


def test_a_swallowed_page_timeout_is_blamed_on_fetch():
    verdict, record = _grade(_pagination_app(1), {})
    assert not verdict.passed, verdict
    assert verdict.failing_nodes == ("fetch",), verdict
    fetches = [e for e in record.steps if e.node_name == "fetch"]
    assert len(fetches) == 3
    assert fetches[0].status == "fail", [e.status for e in fetches]
    assert "retried" not in [e.status for e in fetches]


def test_a_healthy_pagination_loop_is_clean():
    verdict, record = _grade(_pagination_app(None), {})
    assert verdict.passed, verdict
    fetches = [e for e in record.steps if e.node_name == "fetch"]
    assert [e.status for e in fetches] == ["pass", "pass", "pass"]


class _Draft(TypedDict, total=False):
    draft: str
    notes: str


def test_a_writer_critic_overwrite_of_draft_stays_clean():
    """Overwrite is still a retry: the first draft is superseded, the run is clean."""
    calls = {"n": 0}

    def writer(state):
        calls["n"] += 1
        text = "Refunds are accepted within 30 days." if calls["n"] > 1 else "first pass"
        return {"draft": text}

    def critic(state):
        return {"notes": "ship" if "30 days" in state.get("draft", "") else "revise"}

    def route(state):
        return "writer" if state.get("notes") == "revise" else END

    g = StateGraph(_Draft)
    g.add_node("writer", writer)
    g.add_node("critic", critic)
    g.add_edge(START, "writer")
    g.add_edge("writer", "critic")
    g.add_conditional_edges("critic", route, {"writer": "writer", END: END})

    verdict, record = _grade(g.compile(), {})
    assert verdict.passed, verdict
    writers = [e for e in record.steps if e.node_name == "writer"]
    assert [e.status for e in writers] == ["retried", "pass"], [e.status for e in writers]
