"""E4: parallel `Send` workers are siblings, not retries.

`_apply_loop_retries` marks a node's earlier runs `retried` once its last run
passes — right for a writer/critic loop, wrong for fan-out. Two workers that ran
in the same superstep are different work (two names to screen, two line items to
price), so the last one passing says nothing about the first. Grouped by node
name alone, a swallowed tool error in any worker but the last was filed as a
superseded attempt and the run graded clean: a customer onboarded without a
sanctions screen on their primary name, a claim priced with a $0 line item.

Guards on the other side: a real loop still self-corrects (a ReAct agent that
404s, fixes its arguments and succeeds stays clean), and a healthy fan-out stays
clean. Nothing is patched — `patch_graph` raises throughout.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

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
from langgraph.types import Send  # noqa: E402

pytestmark = pytest.mark.integration

NAMES = ["Maria Lopez Garcia", "Maria Lopez", "M. Lopez"]


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


class _Screen(TypedDict, total=False):
    names: list
    screening: Annotated[list, operator.add]
    decision: str


def _screening_app(down: str | None):
    """plan → Send ×3 screen (sanctions tool) → decide. `down`'s screen swallows an outage."""

    @tool
    def sanctions_api(name: str) -> dict:
        """Sanctions screening."""
        if name == down:
            raise ConnectionError("sanctions provider 503")
        return {"hits": [{"list": "PEP", "score": 0.4}]}

    def screen(s):
        try:
            hits = sanctions_api.invoke({"name": s["name"]})["hits"]
        except Exception:
            hits = [{"list": "none", "score": 0.0}]  # swallowed: reads as screened
        return {"screening": [{"name": s["name"], "hits": hits}]}

    g = StateGraph(_Screen)
    g.add_node("screen", screen)
    g.add_node("decide", lambda s: {"decision": "manual_review"})
    g.add_conditional_edges(
        START, lambda s: [Send("screen", {"name": n}) for n in s["names"]], ["screen"]
    )
    g.add_edge("screen", "decide")
    g.add_edge("decide", END)
    return g.compile()


def _grade(app, payload):
    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(app).invoke(payload)
    record = load_run(recorder.session.run_id)
    return evaluate_run(record), record


@pytest.mark.parametrize("down", NAMES, ids=["first", "middle", "last"])
def test_a_swallowed_outage_in_any_fanout_worker_is_blamed(down):
    verdict, record = _grade(_screening_app(down), {"names": NAMES})
    assert not verdict.passed, verdict
    assert verdict.failing_nodes == ("screen",), verdict
    screens = [e for e in record.steps if e.node_name == "screen"]
    assert len(screens) == 3
    assert not any(e.status == "retried" for e in screens), [e.status for e in screens]


def test_siblings_share_a_superstep():
    _, record = _grade(_screening_app(None), {"names": NAMES})
    screens = [e for e in record.steps if e.node_name == "screen"]
    assert len({e.superstep for e in screens}) == 1
    decide = next(e for e in record.steps if e.node_name == "decide")
    assert decide.superstep != screens[0].superstep


def test_a_healthy_fanout_is_clean():
    verdict, record = _grade(_screening_app(None), {"names": NAMES})
    assert verdict.passed, verdict
    assert all(e.status == "pass" for e in record.steps)


class _ToolCallingFake(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def test_a_react_agent_that_recovers_from_a_tool_error_is_still_clean():
    """A real loop: the 404 is superseded by the corrected call. Must stay `retried`."""

    @tool
    def lookup_order(order_id: str) -> dict:
        """Look up an order."""
        if order_id == "A-9":
            return {"status_code": 404, "error": "order A-9 not found"}
        return {"order_id": order_id, "status": "delivered"}

    model = _ToolCallingFake(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_order", "args": {"order_id": "A-9"}, "id": "c1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_order", "args": {"order_id": "A-1"}, "id": "c2"}],
            ),
            AIMessage(content="Order A-1 was delivered."),
        ]
    )
    verdict, record = _grade(
        create_react_agent(model, [lookup_order]), {"messages": [("user", "where is order A-1?")]}
    )
    assert verdict.passed, verdict
    tools = [e.status for e in record.steps if e.node_name == "tools"]
    assert tools == ["retried", "pass"], tools
