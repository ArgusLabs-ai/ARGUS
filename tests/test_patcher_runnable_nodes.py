"""B-8 (F-28): patch_graph must handle StateNodeSpec runnables that are not
RunnableCallables (e.g. RunnableSequence) — wrap the runnable, never replace
the spec (replacing it crashed langgraph validate(): "'function' object has
no attribute 'ends'"). Compiled subgraphs stay unmonitored (#74 semantics).
"""

from __future__ import annotations

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from argus.patcher import patch_graph


class FakeSession:
    def __init__(self):
        self.wrapped: list[str] = []

    def wrap(self, node_name, fn):
        self.wrapped.append(node_name)
        return fn


def test_sequence_node_compiles_and_runs_wrapped():
    """The F-28 repro: add_node('n1', RunnableLambda(...) | RunnableLambda(...))
    then attach — pre-fix this died at compile with AttributeError."""
    seq = RunnableLambda(lambda s: {"x": s["x"] + 1}) | RunnableLambda(
        lambda s: {"x": s["x"] * 10}
    )
    g = StateGraph(dict)
    g.add_node("n1", seq)
    g.add_edge(START, "n1")
    g.add_edge("n1", END)

    session = FakeSession()
    patch_graph(g, session)  # pre-fix: spec replaced by bare function
    app = g.compile()  # pre-fix: AttributeError: 'function' object has no attribute 'ends'
    assert app.invoke({"x": 1}) == {"x": 20}
    assert session.wrapped == ["n1"]


def test_sequence_node_spec_not_replaced():
    """The StateNodeSpec object must survive patching (F-28 root cause)."""
    seq = RunnableLambda(lambda s: {"x": 1}) | RunnableLambda(lambda s: {"x": 2})
    g = StateGraph(dict)
    g.add_node("n1", seq)
    g.add_edge(START, "n1")
    g.add_edge("n1", END)

    session = FakeSession()
    spec_before = g.nodes["n1"]
    patch_graph(g, session)
    assert g.nodes["n1"] is spec_before  # spec intact; only .runnable swapped


def test_compiled_subgraph_node_skipped_not_corrupted():
    """Compiled graphs (subgraph nodes) are left unmonitored (#74): wrapping
    them would defeat langgraph's subgraph special-casing."""
    sub = StateGraph(dict)
    sub.add_node("inner", lambda s: {"x": s["x"] + 1})
    sub.add_edge(START, "inner")
    sub.add_edge("inner", END)
    sub_app = sub.compile()

    g = StateGraph(dict)
    g.add_node("sub", sub_app)
    g.add_edge(START, "sub")
    g.add_edge("sub", END)

    session = FakeSession()
    spec_before = g.nodes["sub"]
    patch_graph(g, session)
    assert g.nodes["sub"] is spec_before
    assert g.nodes["sub"].runnable is sub_app  # untouched
    assert session.wrapped == []
    app = g.compile()
    assert app.invoke({"x": 1}) == {"x": 2}


def test_function_node_still_wrapped_via_func_attr():
    """The RunnableLambda single-node path keeps using .func directly."""
    g = StateGraph(dict)
    g.add_node("n1", RunnableLambda(lambda s: {"x": s["x"] + 1}))
    g.add_edge(START, "n1")
    g.add_edge("n1", END)

    session = FakeSession()
    patch_graph(g, session)
    assert session.wrapped == ["n1"]
    assert g.compile().invoke({"x": 1}) == {"x": 2}
