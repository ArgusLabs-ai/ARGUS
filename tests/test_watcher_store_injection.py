"""Regression tests: attach() must preserve store/cache across recompile.

_attach_compiled recompiles the graph's builder. Previously it carried over
only checkpointer/interrupt_before/interrupt_after, silently dropping
compile(store=...) — nodes then received store=None from langgraph's runtime
injection.
"""

from typing import TypedDict

import pytest

from argus.watcher import ArgusWatcher

pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.store.base import BaseStore  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

_WATCH_KW = dict(semantic_judge=False, investigate=False, record_http=False)


class _S(TypedDict):
    n: int
    seen_store: str


def _store_graph(received: dict) -> StateGraph:
    def node(state: _S, *, store: BaseStore) -> dict:
        received["store"] = store
        return {"n": state["n"] + 1, "seen_store": type(store).__name__}

    g = StateGraph(_S)
    g.add_node("node", node)
    g.add_edge(START, "node")
    g.add_edge("node", END)
    return g


@pytest.mark.unit
def test_attach_compiled_preserves_store():
    received: dict = {}
    app = _store_graph(received).compile(store=InMemoryStore())
    ArgusWatcher(**_WATCH_KW).attach(app)
    result = app.invoke({"n": 0, "seen_store": ""})
    assert isinstance(received["store"], InMemoryStore)
    assert result["seen_store"] == "InMemoryStore"


@pytest.mark.unit
def test_attach_builder_path_preserves_store():
    """Constructor path (uncompiled builder, caller compiles) keeps store too."""
    received: dict = {}
    graph = _store_graph(received)
    ArgusWatcher(graph, **_WATCH_KW)
    app = graph.compile(store=InMemoryStore())
    app.invoke({"n": 0, "seen_store": ""})
    assert isinstance(received["store"], InMemoryStore)
