"""Does the pivot path hold up on the shapes people actually ship?

Companion to ``test_silent_failure_matrix.py``. That file stress-tests the
detection *core* over seven hand-built topologies driven by ``invoke``. This one
covers the three things it explicitly did not, each of which turned out to hide
a defect:

* **``create_react_agent``** — the prebuilt, on ``MessagesState``, with a real
  tool-call round trip. Its message envelopes and stringified tool results broke
  three separate rules at once.
* **``MessagesState`` / ``add_messages``** — the state class almost every chat
  or agent graph uses. Its reducer was folding as *overwrite*.
* **``.batch()`` and repeat ``invoke``** — one ``attach``, many runs. Only the
  first was ever graded.

Same contract as the sibling matrix: ``patch_graph`` is monkeypatched to raise,
and every test asserts **the blamed node**, not just that something was found.
Roughly half assert a pipeline is **clean** — the false-positive line is the
harder half to hold, since a CI gate that fails working pipelines is worse than
no gate.

Deterministic throughout: the model is a scripted fake, so a failure here is a
detection bug and never a flaky model. Live-model coverage is out of scope for
CI and lives outside this suite.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest

from argus.check import evaluate_run
from argus.ledger import build_ledger
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402
from langgraph.types import Command, Send  # noqa: E402
from typing_extensions import TypedDict  # noqa: E402

pytestmark = pytest.mark.integration


# ── harness ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _unwrapped(monkeypatch):
    """The pivot path must never touch the graph engine."""

    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — the pivot path wraps nothing")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _run(app, payload, consumers=None):
    """Attach, invoke, return (verdict, record, ledger rows by node)."""
    recorder = ArgusRecorder(consumers=consumers, semantic_judge=False)
    recorder.attach(app).invoke(payload)
    record = load_run(recorder.session.run_id)
    rows = build_ledger(record.steps, record.initial_state, record.reducer_kinds)
    return evaluate_run(record), record, {r.node: r for r in rows}


# ── 1. create_react_agent ────────────────────────────────────────────────────


class _ToolCallingFake(FakeMessagesListChatModel):
    """A scripted chat model that survives ``bind_tools``.

    The stock fakes raise ``NotImplementedError`` on ``bind_tools``, which
    ``create_react_agent`` calls. Binding is a no-op — the scripted replies
    already carry the tool calls.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


@tool
def _lookup_order(order_id: str) -> dict:
    """Look up an order by id."""
    return {"order_id": order_id, "status": "delivered", "carrier": "DHL"}


@tool
def _broken_lookup(order_id: str) -> dict:
    """Look up an order by id against a backend that is down."""
    return {"error": "upstream returned 500", "status_code": 500}


def _agent(tool_fn, final: str):
    model = _ToolCallingFake(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": tool_fn.name, "args": {"order_id": "A-1"}, "id": "c1"}],
            ),
            AIMessage(content=final),
        ]
    )
    return create_react_agent(model, [tool_fn])


_ASK = {"messages": [("user", "where is order A-1?")]}


def test_a_healthy_react_agent_is_clean():
    """The load-bearing false-positive test for the whole prebuilt path.

    Three separate rules used to fail this run: ``empty_result`` on the message
    envelope's empty ``response_metadata``, ``malformed_payload`` on a valid
    JSON tool result, and phantom duplicate ``agent`` rows. A gate that fails
    every working react agent is a gate nobody can turn on.
    """
    verdict, _, rows = _run(_agent(_lookup_order, "Order A-1 was delivered by DHL."), _ASK)
    assert verdict.passed, verdict
    assert {"agent", "tools"} <= set(rows), sorted(rows)


def test_a_react_agent_files_one_row_per_turn():
    """No phantom steps: two model turns and one tool call, not more.

    A duplicate row carries no update, and the duplicates used to push the real
    rows into ``retried`` — a status ``argus check`` skips outright.
    """
    _, record, _ = _run(_agent(_lookup_order, "Order A-1 was delivered by DHL."), _ASK)
    nodes = [s.node_name for s in record.steps if s.status != "skipped"]
    assert nodes == ["agent", "tools", "agent"], nodes
    assert all(s.output_dict is not None for s in record.steps), nodes


def test_the_react_tool_step_reaches_the_ledger_with_its_io():
    """A verdict on an agent is worthless if the tool round-trip is invisible."""
    _, _, rows = _run(_agent(_lookup_order, "Order A-1 was delivered by DHL."), _ASK)
    assert rows["tools"].update, "the tools node's update never reached the notebook"
    assert "delivered" in str(rows["tools"].update)


def test_a_swallowed_tool_error_in_a_react_agent_fails_the_gate():
    """The backend 500s, the tool returns the error, the model answers anyway.

    LangChain stringifies a structured tool return into ``ToolMessage.content``,
    so the error payload arrives as JSON *inside a string*. Without decoding it
    the flagship silent failure ships clean through the prebuilt agent.
    """
    verdict, _, _ = _run(_agent(_broken_lookup, "Order A-1 was delivered by DHL."), _ASK)
    assert not verdict.passed, "an error payload flowing through a tool must fail the gate"
    assert "tools" in verdict.failing_nodes, verdict


def test_a_react_agent_that_answers_nothing_is_caught():
    """The loop finishes and the final message is a refusal with no content."""
    verdict, _, _ = _run(_agent(_lookup_order, "I don't have that information."), _ASK)
    assert not verdict.passed, verdict
    assert "agent" in verdict.failing_nodes, verdict


# ── 2. MessagesState / add_messages ──────────────────────────────────────────


def _chat_graph(reply):
    """receive → respond → log over the stock ``MessagesState``.

    ``respond`` is deliberately not terminal: ``empty_output`` is gated on a
    node having successors, so a last node is exempt by design and would not
    exercise the rule.
    """

    def receive(state: MessagesState) -> dict:
        return {"messages": [HumanMessage(content="acknowledged")]}

    def log(state: MessagesState) -> dict:
        return {"messages": [AIMessage(content="delivered to the customer")]}

    g = StateGraph(MessagesState)
    g.add_node("receive", receive)
    g.add_node("respond", reply)
    g.add_node("log", log)
    g.add_edge(START, "receive")
    g.add_edge("receive", "respond")
    g.add_edge("respond", "log")
    g.add_edge("log", END)
    return g.compile()


_CHAT = {"messages": [HumanMessage(content="what is our refund window?")]}


def _answering(text: str):
    def respond(state: MessagesState) -> dict:
        return {"messages": [AIMessage(content=text)]}

    return respond


def test_add_messages_accumulates_in_the_ledger():
    """The notebook's running state must grow, not overwrite.

    ``add_messages`` is exported under that name but the callable on the
    annotation is ``_add_messages``, so a raw ``__name__`` lookup folded it as
    overwrite — every row after a message-producing node showed one message and
    contextual blame read a state that never existed.
    """
    verdict, record, _ = _run(
        _chat_graph(_answering("Refunds are accepted within 30 days.")), _CHAT
    )
    assert verdict.passed, verdict
    rows = build_ledger(record.steps, record.initial_state, record.reducer_kinds)
    counts = [len(row.state_after["messages"]) for row in rows]
    assert counts == [2, 3, 4], f"add_messages did not accumulate: {counts}"


def test_an_add_messages_update_is_the_update_not_the_merged_pile():
    """Each row's ``update`` holds only what that node returned."""
    _, _, rows = _run(_chat_graph(_answering("Refunds are accepted within 30 days.")), _CHAT)
    assert len(rows["respond"].update["messages"]) == 1, rows["respond"].update


def test_a_message_node_that_returns_nothing_is_blamed():
    """The ``{}`` no-op must stay visible under ``MessagesState``."""

    def respond(state: MessagesState) -> dict:
        _ = "generated, then dropped"
        return {}

    verdict, _, _ = _run(_chat_graph(respond), _CHAT)
    assert not verdict.passed, verdict
    assert verdict.failing_nodes == ("respond",), verdict


def test_a_refusal_as_the_whole_reply_fails_the_gate():
    """The commonest agent silent failure: it answers, but answers nothing.

    The promotion that makes a whole-value placeholder critical had to learn to
    walk ``messages.[0].content`` — a path with a list hop in it — or it could
    never apply to a message-based state, which is to say to any agent.
    """
    verdict, _, _ = _run(_chat_graph(_answering("I don't have that information.")), _CHAT)
    assert not verdict.passed, verdict
    assert "respond" in verdict.failing_nodes, verdict


def test_a_node_that_echoes_user_text_is_not_blamed_for_its_wording():
    """The false-positive line for pass-through input.

    A support ticket that reads "I cannot reset my password" is a real customer,
    not a model refusing. Scanning every string in an update — including one the
    node was handed and passed on unchanged — failed the build on the customer's
    own words, on every desk / chat pipeline there is.
    """

    class State(TypedDict, total=False):
        ticket: str
        category: str

    def classify(state: State) -> dict:
        return {"ticket": state["ticket"].strip()}

    def route(state: State) -> dict:
        return {"category": "access"}

    g = StateGraph(State)
    g.add_node("classify", classify)
    g.add_node("route", route)
    g.add_edge(START, "classify")
    g.add_edge("classify", "route")
    g.add_edge("route", END)

    verdict, _, _ = _run(g.compile(), {"ticket": "  I cannot reset my password  "})
    assert verdict.passed, verdict


def test_an_updated_message_is_not_double_counted():
    """PINNED CEILING — ``ledger._ADD_REDUCERS`` treats ``add_messages`` as
    concatenation, but it really de-duplicates by message id.

    If this starts failing, the fold learned real ``add_messages`` semantics and
    the docs should say so.
    """

    def respond(state: MessagesState) -> dict:
        first = state["messages"][0]
        return {"messages": [HumanMessage(content="corrected question", id=first.id)]}

    _, record, _ = _run(_chat_graph(respond), _CHAT)
    rows = build_ledger(record.steps, record.initial_state, record.reducer_kinds)
    assert len(rows[-1].state_after["messages"]) == 4, rows[-1].state_after["messages"]


def test_a_silent_first_turn_of_a_loop_is_exempt_from_the_gate():
    """PINNED BEHAVIOUR, not an endorsement — see ``docs/STATUS.md``.

    ``session._apply_loop_retries`` relabels every earlier iteration of a looped
    node ``retried`` whenever the final one passed, whatever those earlier
    iterations did, and ``check.evaluate_run`` skips ``retried``. So a worker
    that returns ``{}`` on round 1 and recovers on round 2 ships clean.

    Right for a genuine retry, wrong for an accumulating field: with
    ``add_messages`` round 1's empty contribution is never superseded, it is
    simply missing. Pinned so the decision stays explicit.
    """
    calls = {"n": 0}

    def worker(state: MessagesState) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {}
        return {"messages": [AIMessage(content="Refunds are accepted within 30 days.")]}

    def route(state: MessagesState) -> str:
        return "worker" if calls["n"] < 2 else "done"

    def done(state: MessagesState) -> dict:
        return {"messages": [AIMessage(content="sent to customer")]}

    g = StateGraph(MessagesState)
    g.add_node("worker", worker)
    g.add_node("done", done)
    g.add_edge(START, "worker")
    g.add_conditional_edges("worker", route, {"worker": "worker", "done": "done"})
    g.add_edge("done", END)

    verdict, record, _ = _run(g.compile(), _CHAT)
    statuses = [(s.node_name, s.status) for s in record.steps]
    assert statuses[0] == ("worker", "retried"), statuses
    assert verdict.passed, verdict  # ← the gap: a silent round 1 ships


# ── 3. one attach, many runs ─────────────────────────────────────────────────


def _desk_graph(retrieve):
    """classify → retrieve → draft, so ``retrieve`` has a successor waiting."""

    class State(TypedDict, total=False):
        ticket: str
        docs: list
        draft: str

    def classify(state: State) -> dict:
        return {"ticket": state["ticket"].strip()}

    def draft(state: State) -> dict:
        return {"draft": f"Based on {len(state.get('docs') or [])} article(s)."}

    g = StateGraph(State)
    g.add_node("classify", classify)
    g.add_node("retrieve", retrieve)
    g.add_node("draft", draft)
    g.add_edge(START, "classify")
    g.add_edge("classify", "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", END)
    return g.compile()


def _healthy_retrieve(state):
    return {"docs": ["kb-101", "kb-233"]}


def _silent_retrieve(state):
    """Searched, found matches, threw them away."""
    _ = ["kb-101", "kb-233"]
    return {}


_TICKET = {"ticket": "  how do I reset my password  "}


def test_repeat_invoke_on_one_recorder_records_both_runs():
    """Attach once, invoke twice — a served app, a notebook, an eval loop.

    The second invoke used to append its steps to an already-finalized session:
    never saved, never graded, and no error to say so.
    """
    recorder = ArgusRecorder(semantic_judge=False)
    bound = recorder.attach(_desk_graph(_silent_retrieve))
    bound.invoke(_TICKET)
    first = recorder.session.run_id
    bound.invoke({"ticket": "second ticket"})
    second = recorder.session.run_id

    assert second != first, "the second invoke reused the first run — one verdict for two runs"
    for run_id in (first, second):
        verdict = evaluate_run(load_run(run_id))
        assert not verdict.passed and verdict.failing_nodes == ("retrieve",), verdict


def test_batch_grades_every_item_as_its_own_run():
    """``.batch()`` runs its items on parallel threads under one recorder.

    Folding them into one notebook makes the running state a merge of two
    different inputs, so a field written by item B reads as present for item A.
    """
    recorder = ArgusRecorder(semantic_judge=False)
    bound = recorder.attach(_desk_graph(_silent_retrieve))
    bound.batch([_TICKET, {"ticket": "billing question"}])

    assert len(recorder.run_ids) == 2, f"batch lost an item: {recorder.run_ids}"
    for run_id in recorder.run_ids:
        record = load_run(run_id)
        nodes = [s.node_name for s in record.steps if s.status != "skipped"]
        assert nodes == ["classify", "retrieve", "draft"], nodes
        verdict = evaluate_run(record)
        assert not verdict.passed and verdict.failing_nodes == ("retrieve",), verdict


def test_batch_does_not_leak_one_items_state_into_another():
    """Item A is healthy, item B is silent. Only B's run may fail."""

    def picky_retrieve(state):
        return {} if "billing" in state["ticket"] else {"docs": ["kb-101"]}

    recorder = ArgusRecorder(semantic_judge=False)
    bound = recorder.attach(_desk_graph(picky_retrieve))
    bound.batch([_TICKET, {"ticket": "billing question"}])

    verdicts = {}
    for run_id in recorder.run_ids:
        record = load_run(run_id)
        verdicts[record.initial_state["ticket"].strip()] = evaluate_run(record)
    assert verdicts["how do I reset my password"].passed, verdicts
    assert not verdicts["billing question"].passed, verdicts
    assert verdicts["billing question"].failing_nodes == ("retrieve",), verdicts


def test_streaming_grades_the_same_as_invoke():
    """``.stream()`` must reach the same verdict and blame the same node."""
    recorder = ArgusRecorder(semantic_judge=False)
    list(recorder.attach(_desk_graph(_silent_retrieve)).stream(_TICKET))
    verdict = evaluate_run(load_run(recorder.session.run_id))
    assert not verdict.passed and verdict.failing_nodes == ("retrieve",), verdict


def test_streaming_a_healthy_graph_stays_clean():
    recorder = ArgusRecorder(semantic_judge=False)
    list(recorder.attach(_desk_graph(_healthy_retrieve)).stream(_TICKET))
    assert evaluate_run(load_run(recorder.session.run_id)).passed


# ── 4. the judge must not fail a run the rules were silent about ─────────────


def _judge_says_fail(**kwargs):
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "pass": False,
                            "reason": "the content field is empty",
                            "confidence": 1.0,
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _run_judged(app, payload):
    recorder = ArgusRecorder(semantic_judge=True)
    recorder.attach(app).invoke(payload)
    return evaluate_run(load_run(recorder.session.run_id))


def test_a_healthy_agent_survives_a_judge_that_wants_to_fail_it(monkeypatch):
    """The judge rules on evidence; with none it annotates and nothing more.

    Free to originate failures, the judge made the gate nondeterministic — the
    same healthy `create_react_agent` failed two runs in three against a live
    model, at confidence 1.0, with contradictory reasons ("the output contains
    a valid response but is missing a required field"). A gate that red-lights
    working pipelines at random gets switched off.

    The mock always votes fail, so this pins the rule rather than the weather.
    """
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _judge_says_fail)

    verdict = _run_judged(_agent(_lookup_order, "Order A-1 was delivered by DHL."), _ASK)
    assert verdict.passed, verdict


def test_a_real_failure_still_fails_with_the_judge_on(monkeypatch):
    """The corroboration rule must not blunt a genuine catch."""
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _judge_says_fail)

    verdict = _run_judged(_agent(_broken_lookup, "Order A-1 was delivered by DHL."), _ASK)
    assert not verdict.passed, verdict
    assert "tools" in verdict.failing_nodes, verdict


def test_a_tool_call_turn_is_not_judged_as_an_empty_answer(monkeypatch):
    """A turn that only issues tool calls has no prose to rule on.

    Asked anyway, the judge answers "the content field is empty, so the output
    is not semantically relevant" — on every agent turn there is.
    """
    from argus.semantic_checker import check_semantic_coherence

    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _judge_says_fail)

    tool_turn = {"messages": [{"type": "ai", "content": "", "tool_calls": [{"name": "t"}]}]}
    result, _ = check_semantic_coherence("agent", {"messages": ["ask"]}, tool_turn)
    assert result.evaluated is False, result
    assert "tool-call turn" in result.reason


# ── 5. coherence: the one verdict the judge may reach on its own ─────────────


def _judge_verdict(**fields):
    payload = {"pass": False, "reason": "r", "confidence": 1.0, **fields}

    def _call(**kwargs):
        return {
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    return _call


def _cake_graph(method):
    """gather → make_method → plate. Structurally perfect in every variant."""

    class Kitchen(TypedDict, total=False):
        ingredients: list
        method: str
        plated: str

    def gather(state):
        return {"ingredients": ["flour", "sugar", "eggs"]}

    def plate(state):
        return {"plated": f"serves 8: {state['method'][:40]}"}

    g = StateGraph(Kitchen)
    g.add_node("gather", gather)
    g.add_node("make_method", method)
    g.add_node("plate", plate)
    g.add_edge(START, "gather")
    g.add_edge("gather", "make_method")
    g.add_edge("make_method", "plate")
    g.add_edge("plate", END)
    return g.compile()


_ON_TOPIC = lambda s: {"method": "Cream the butter and sugar, fold in the flour, bake 25 min."}  # noqa: E731


def test_an_unrelated_verdict_fails_the_run_with_no_rule_agreeing(monkeypatch):
    """Cake in, helicopters out: no rule can see this, so the judge stands alone.

    Nothing is missing, empty, malformed or erroring — every deterministic layer
    passes. If the judge could not gate here, ARGUS would have no answer at all
    for "is the node doing the right job?".
    """
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    monkeypatch.setattr(
        "argus.llm_proxy.create_chat_completion",
        _judge_verdict(failure_kind="unrelated", reason="output is about helicopters"),
    )
    verdict = _run_judged(_cake_graph(_ON_TOPIC), {})
    assert not verdict.passed, verdict


def test_an_empty_or_missing_verdict_does_not_gate_on_its_own(monkeypatch):
    """Emptiness is the rules' job; the judge only gets a vote alongside them."""
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    monkeypatch.setattr(
        "argus.llm_proxy.create_chat_completion",
        _judge_verdict(failure_kind="empty_or_missing"),
    )
    assert _run_judged(_cake_graph(_ON_TOPIC), {}).passed


def test_a_coherence_verdict_that_does_not_reproduce_is_demoted(monkeypatch):
    """Standing alone means proving it twice.

    The first call says "unrelated", the confirmation disagrees. An intermittent
    misread must not fail a build — a real mismatch of subject reproduces.
    """
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    calls = {"n": 0}

    def _flip_flop(**kwargs):
        calls["n"] += 1
        first = calls["n"] == 1
        payload = (
            {"pass": False, "reason": "unrelated", "confidence": 1.0, "failure_kind": "unrelated"}
            if first
            else {"pass": True, "reason": "fine", "confidence": 1.0, "failure_kind": "other"}
        )
        return {
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _flip_flop)
    assert _run_judged(_cake_graph(_ON_TOPIC), {}).passed
    assert calls["n"] >= 2, "the confirmation call never happened"


# ── 4. the graph composed into something bigger ──────────────────────────────


class _ComposedState(TypedDict, total=False):
    a: str


def _silent_then_writing_graph():
    """`n1` no-ops with `n2` waiting — `empty_output` territory."""
    g = StateGraph(_ComposedState)
    g.add_node("n1", lambda s: {})
    g.add_node("n2", lambda s: {"a": "x"})
    g.add_edge(START, "n1")
    g.add_edge("n1", "n2")
    g.add_edge("n2", END)
    return g.compile()


@pytest.mark.parametrize(
    "compose",
    [
        pytest.param(lambda app: app, id="direct"),
        pytest.param(lambda app: RunnableLambda(lambda x: x) | app, id="lambda|app"),
        pytest.param(
            lambda app: RunnableLambda(lambda x: x) | app | RunnableLambda(lambda x: x),
            id="lambda|app|lambda",
        ),
        pytest.param(
            lambda app: (RunnableLambda(lambda x: x) | RunnableLambda(lambda x: x)) | app,
            id="nested-sequence|app",
        ),
    ],
)
def test_a_composed_graph_is_still_graded(compose):
    """The graph is one step of something larger — still one run, still blamed (#87).

    `attach` used to return `app.with_config(callbacks=[self])`. LangGraph's
    `ensure_config` overwrites the callbacks key rather than merging it, so the
    handler was dropped the moment a caller passed its own — which composition
    always does. ARGUS then recorded nothing and, never reaching `_finish`,
    said nothing either: the silent pass the brief bans, arriving through the
    front door.
    """
    recorder = ArgusRecorder(semantic_judge=False)
    compose(recorder.attach(_silent_then_writing_graph())).invoke({})

    assert recorder.run_ids, "a composed graph must still produce a run"
    record = load_run(recorder.run_ids[-1])
    assert record.first_failure_step == "n1"
    assert {s.node_name for s in record.steps} == {"n1", "n2"}


def test_a_composed_graph_records_one_run_per_call():
    """Composition must not cost the per-call runs `.batch()` and reuse rely on."""
    recorder = ArgusRecorder(semantic_judge=False)
    chain = RunnableLambda(lambda x: x) | recorder.attach(_silent_then_writing_graph())

    chain.invoke({})
    chain.batch([{}, {}])
    list(chain.stream({}))

    assert len(recorder.run_ids) == 4, f"one run per call, got {recorder.run_ids}"
    assert all(load_run(rid).first_failure_step == "n1" for rid in recorder.run_ids)


def test_attach_still_hands_back_the_graph_api():
    """Callers get a graph, not an opaque wrapper — `argus replay` reads `.nodes`."""
    recorder = ArgusRecorder(semantic_judge=False)
    attached = recorder.attach(_silent_then_writing_graph())

    for attr in ("nodes", "get_graph", "invoke", "stream", "batch"):
        assert hasattr(attached, attr), f"attach() dropped `{attr}`"


# ── 6. Command handoffs ──────────────────────────────────────────────────────
#
# `Command(goto=..., update={...})` is the modern LangGraph handoff idiom and
# what every multi-agent / supervisor example now emits. It is not a dict, so
# `_close_step` used to discard the update and file the step as "unreadable"
# (#88): `empty_output` could not fire, the ledger row had no `update`, and the
# consumer map blamed whoever came next. The run graded clean either way.


class _HandoffState(TypedDict, total=False):
    plan: str
    reply: str


def _handoff_graph(supervise):
    """`supervise` hands off with a Command; `write` reads `plan` and answers."""

    def write(state: _HandoffState) -> dict:
        return {"reply": f"written from {state.get('plan') or 'nothing'}"}

    graph = StateGraph(_HandoffState)
    graph.add_node("supervise", supervise)
    graph.add_node("write", write)
    graph.add_edge(START, "supervise")
    graph.add_edge("write", END)
    return graph.compile()


def test_a_command_update_reaches_the_ledger():
    """The load-bearing one: what the node wrote must be in the notebook."""

    def supervise(state: _HandoffState) -> Command:
        return Command(goto="write", update={"plan": "outline"})

    verdict, _, rows = _run(_handoff_graph(supervise), {})
    assert rows["supervise"].update == {"plan": "outline"}, rows["supervise"].update
    assert verdict.passed, verdict


def test_a_command_with_an_empty_update_is_blamed():
    """`Command(goto=..., update={})` is the canonical silent no-op."""

    def supervise(state: _HandoffState) -> Command:
        _ = "planned, then dropped"
        return Command(goto="write", update={})

    verdict, record, _ = _run(_handoff_graph(supervise), {})
    assert not verdict.passed, verdict
    assert verdict.failing_nodes == ("supervise",), verdict
    assert record.first_failure_step == "supervise"


def test_a_routing_only_command_is_not_blamed():
    """A supervisor that only routes wrote nothing on purpose — not a failure.

    The false-positive half, and deliberately the *same* graph and payload as
    the test above: the only difference is `update={}` versus no update at all.
    `update=None` is "I claim no update", which is not "I claim an empty one",
    and every supervisor pattern emits it. Collapsing the two (`update or {}`)
    is the tempting wrong fix — it fails this test and nothing else.
    """

    def supervise(state: _HandoffState) -> Command:
        return Command(goto="write")

    verdict, _, rows = _run(_handoff_graph(supervise), {})
    assert verdict.passed, verdict
    assert rows["supervise"].update is None, rows["supervise"].update


def test_consumers_blame_the_command_node_that_dropped_the_field():
    """The consequence the issue names: blame lands on the writer, not the reader."""

    def supervise(state: _HandoffState) -> Command:
        return Command(goto="write", update={"reply": "", "plan": ""})

    verdict, record, _ = _run(
        _handoff_graph(supervise), {}, consumers={"plan": ["write"]}
    )
    assert not verdict.passed, verdict
    assert record.first_failure_step == "supervise", record.first_failure_step


def test_a_command_fan_out_keeps_its_update():
    """`Command(goto=[Send(...)])` — the fan-out shape — must not lose its update."""

    def supervise(state: _HandoffState) -> Command:
        return Command(goto=[Send("write", {"plan": "outline"})], update={"plan": "outline"})

    _, _, rows = _run(_handoff_graph(supervise), {})
    assert rows["supervise"].update == {"plan": "outline"}, rows["supervise"].update


def test_an_annotated_handoff_is_blamed_by_empty_output_itself():
    """The shape real supervisors ship: `-> Command[Literal["write"]]`.

    The annotation is what lets LangGraph draw the `supervise -> write` edge, so
    this is the one Command test running against a *true* edge map rather than
    the degenerate `supervise -> __end__` one. Asserting the finding by name
    stops the test passing for some incidental reason.
    """

    def supervise(state: _HandoffState) -> Command[Literal["write"]]:
        _ = "planned, then dropped"
        return Command(goto="write", update={})

    verdict, record, _ = _run(_handoff_graph(supervise), {})
    assert not verdict.passed, verdict
    empty = [f for f in record.findings if f.type == "empty_output"]
    assert [f.node for f in empty] == ["supervise"], record.findings


def test_odd_but_legal_command_shapes_never_take_the_graph_down():
    """A recorder that raises takes the user's pipeline with it.

    Each of these is accepted by LangGraph, so ARGUS will meet them in the
    wild. The contract is narrow on purpose: record a run, do not raise. A
    pair-sequence update is a real update in a different shape and is folded;
    the rest may legitimately read as "no update", but never as a traceback.
    """
    cases = {
        "pairs": (lambda s: Command(goto="write", update=[("plan", "x")]), {"plan": "x"}),
        "no goto": (lambda s: Command(update={"plan": "x"}), {"plan": "x"}),
        "returns None": (lambda s: None, None),
        "unfoldable update": (lambda s: Command(goto="write", update=["plan"]), None),
    }
    for label, (supervise, expected) in cases.items():
        _, _, rows = _run(_handoff_graph(supervise), {})
        assert rows["supervise"].update == expected, f"{label}: {rows['supervise'].update}"
