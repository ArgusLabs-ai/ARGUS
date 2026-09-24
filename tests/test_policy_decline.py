"""E3 / #130: a short legitimate policy decline is not a hard fail.

BA-004 promotes a refusal phrase in a short main answer to critical. That
catches ``I'm unable to answer questions about company revenue`` and also a
support agent correctly declining a refund after a lookup showed the order
was outside the return window. The decline cites the constraint (order id,
45 days, an alternative), so it is a warning the reviewer can see. It does
not fail ``argus check``. The bare refusal stays critical. The judge is not
asked to originate that fail or to clear it.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from argus.check import evaluate_run
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402

pytestmark = pytest.mark.integration

_POLICY = (
    "I'm sorry, but I can't refund order A-1001 because it was delivered 45 days ago, "
    "outside our 30-day return window. I can offer store credit instead."
)
_SQL_REFUSAL = "I'm unable to answer questions about company revenue."


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


class _ToolCallingFake(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _grade(app, payload):
    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(app).invoke(payload)
    record = load_run(recorder.session.run_id)
    return evaluate_run(record), record


def test_a_policy_decline_after_lookup_does_not_fail_the_gate():
    """Clean: the decline cites the 45-day lookup. Warning, not a CI fail."""

    @tool
    def lookup_order(order_id: str) -> dict:
        """Look up an order."""
        return {"order_id": order_id, "delivered_days_ago": 45, "window_days": 30}

    model = _ToolCallingFake(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_order", "args": {"order_id": "A-1001"}, "id": "c1"}],
            ),
            AIMessage(content=_POLICY),
        ]
    )
    verdict, record = _grade(
        create_react_agent(model, [lookup_order]),
        {"messages": [("user", "please refund order A-1001")]},
    )
    assert verdict.passed, verdict
    warnings = [
        f
        for f in record.findings
        if f.node == "agent" and f.type == "BA-004" and f.severity == "warning"
    ]
    assert warnings, record.findings
    assert not any(f.severity == "critical" for f in record.findings), record.findings


def test_a_bare_text_to_sql_refusal_fails_the_gate():
    """Fails: no cited constraint, so the short refusal stays critical."""

    class State(TypedDict, total=False):
        question: str
        answer: str

    def answer(state: State) -> dict:
        return {"answer": _SQL_REFUSAL}

    g = StateGraph(State)
    g.add_node("answer", answer)
    g.add_edge(START, "answer")
    g.add_edge("answer", END)

    verdict, record = _grade(g.compile(), {"question": "what was company revenue in 2024?"})
    assert not verdict.passed, verdict
    assert verdict.failing_nodes == ("answer",), verdict
    assert any(f.type == "BA-004" and f.severity == "critical" for f in record.findings)
