"""``has_cycles`` against graphs whose acyclicity was decided independently.

The expected answers for the first seven graphs were computed with Jacobian's
``graph.directed.acyclic_order.compute`` (NetworkX, exact): ``acyclic`` is true
exactly when ARGUS must report no cycle. Jacobian only admits *simple* graphs, so
the self-loop case records ARGUS's own convention: a node routing back to itself
is a cycle.
"""

from __future__ import annotations

import pytest

from argus.utils.cycle_detection import has_cycles

# (vertex_count, edges, has_cycle)
_CASES = {
    "diamond DAG": (4, [(0, 1), (0, 2), (1, 3), (2, 3)], False),
    "three-cycle": (3, [(0, 1), (1, 2), (2, 0)], True),
    "two components, one cyclic": (4, [(0, 1), (2, 3), (3, 2)], True),
    "cycle only reached from a later start node": (4, [(0, 1), (2, 1), (1, 3), (3, 2)], True),
    "reverse-numbered DAG": (4, [(3, 2), (2, 1), (1, 0)], False),
    "diamond with a back edge": (4, [(0, 1), (0, 2), (1, 3), (2, 3), (3, 1)], True),
    "two paths into a shared sink": (6, [(0, 1), (1, 2), (2, 5), (3, 4), (4, 5), (0, 3)], False),
    "self-loop (ARGUS convention)": (2, [(0, 0), (0, 1)], True),
}


def _edge_map(vertex_count, edges):
    edge_map = {f"n{i}": [] for i in range(vertex_count)}
    for src, dst in edges:
        edge_map[f"n{src}"].append(f"n{dst}")
    return edge_map


@pytest.mark.parametrize("name", list(_CASES))
def test_has_cycles_matches_the_independent_oracle(name):
    vertex_count, edges, expected = _CASES[name]
    assert has_cycles(_edge_map(vertex_count, edges)) is expected


def test_the_result_does_not_depend_on_node_numbering():
    for vertex_count, edges, expected in _CASES.values():
        relabelled = [(vertex_count - 1 - a, vertex_count - 1 - b) for a, b in edges]
        assert has_cycles(_edge_map(vertex_count, relabelled)) is expected


def test_a_successor_that_is_not_a_declared_node_does_not_create_a_cycle():
    assert has_cycles({"a": ["b"], "b": ["ghost"]}) is False
