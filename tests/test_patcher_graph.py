"""Regression tests for patch_graph node handling."""

from types import SimpleNamespace

import pytest

from argus.patcher import patch_graph


class _StubSession:
    def __init__(self):
        self.wrapped = []

    def wrap(self, name, fn):
        self.wrapped.append(name)

        def _wrapped(*args, **kwargs):
            return fn(*args, **kwargs)

        return _wrapped


def _graph(nodes):
    return SimpleNamespace(nodes=nodes)


@pytest.mark.unit
def test_patch_graph_wraps_plain_callable_node():
    def node_fn(state):
        return state

    graph = _graph({"a": node_fn})
    session = _StubSession()
    patch_graph(graph, session)
    assert session.wrapped == ["a"]
    assert callable(graph.nodes["a"])


@pytest.mark.unit
def test_patch_graph_wraps_nodespec_func_in_place():
    def node_fn(state):
        return state

    spec = SimpleNamespace(runnable=SimpleNamespace(func=node_fn, afunc=None))
    graph = _graph({"a": spec})
    session = _StubSession()
    patch_graph(graph, session)
    assert session.wrapped == ["a"]
    assert graph.nodes["a"] is spec
    assert graph.nodes["a"].runnable.func is not node_fn


@pytest.mark.unit
def test_patch_graph_skips_compiled_subgraph_node():
    # StateNodeSpec whose .runnable is a compiled graph (subgraph nodes, e.g.
    # langgraph-swarm agents, langgraph-reflection) has no .func to wrap.
    # Regression: the legacy fallback replaced the spec with a plain function,
    # corrupting graph.nodes and crashing langgraph validate() at compile.
    compiled = SimpleNamespace(invoke=lambda state: state)
    spec = SimpleNamespace(runnable=compiled)
    graph = _graph({"sub": spec})
    session = _StubSession()
    patch_graph(graph, session)
    assert session.wrapped == []
    assert graph.nodes["sub"] is spec
