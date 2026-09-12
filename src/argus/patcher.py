from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argus.session import ArgusSession


def extract_fn(node_value: Any) -> Any:
    """Extract the underlying callable from a node value.

    Handles both plain callables (legacy) and LangGraph 0.2+ StateNodeSpec objects.
    """
    # LangGraph 0.2+: StateNodeSpec with .runnable.func
    if hasattr(node_value, "runnable") and hasattr(node_value.runnable, "func"):
        return node_value.runnable.func
    return node_value


def patch_graph(graph: Any, session: ArgusSession) -> None:
    """Replace every node function in graph.nodes with a monitoring wrapper.

    Must be called before graph.compile(). Delegates wrapper creation to
    session.wrap() so that GraphInterrupt handling and validator execution
    are centralised in ArgusSession.
    """
    if not hasattr(graph, "nodes"):
        raise AttributeError(
            "argus: graph has no 'nodes' attribute. "
            "Ensure you are passing a LangGraph StateGraph before calling compile(). "
            "Requires langgraph>=0.2.0."
        )

    node_names = list(graph.nodes.keys())
    for node_name in node_names:
        node_value = graph.nodes[node_name]

        # LangGraph 0.2+: nodes are StateNodeSpec; patch the inner .runnable.func
        if hasattr(node_value, "runnable") and hasattr(node_value.runnable, "func"):
            original_fn = node_value.runnable.func
            if original_fn is not None:
                node_value.runnable.func = session.wrap(node_name, original_fn)
            # handle dedicated async func if present
            afunc = node_value.runnable.afunc
            if afunc is not None and afunc is not original_fn:
                node_value.runnable.afunc = session.wrap(node_name, afunc)
        elif not hasattr(node_value, "runnable"):
            # Legacy: nodes are plain callables
            graph.nodes[node_name] = session.wrap(node_name, node_value)
        # StateNodeSpec whose .runnable is a compiled graph (subgraph nodes,
        # e.g. langgraph-swarm agents, langgraph-reflection) has no .func to
        # wrap — leave it unmonitored rather than corrupting graph.nodes.


def extract_edge_map(graph: Any) -> dict[str, list[str]]:
    """Build a {source_node: [dest_nodes]} map from the graph's edge definitions."""
    edge_map: dict[str, list[str]] = {}

    # Standard edges: list of (src, dst) tuples or similar
    edges = getattr(graph, "edges", None) or []
    for edge in edges:
        if isinstance(edge, (tuple, list)) and len(edge) >= 2:
            src, dst = str(edge[0]), str(edge[1])
            edge_map.setdefault(src, [])
            if dst not in edge_map[src]:
                edge_map[src].append(dst)

    # Conditional edges — LangGraph ≥0.2 stores these in graph.branches
    # Structure: {source_node: {branch_name: BranchSpec(ends={value: dest_node})}}
    branches = getattr(graph, "branches", None) or {}
    for src, branch_dict in branches.items():
        src = str(src)
        edge_map.setdefault(src, [])
        if isinstance(branch_dict, dict):
            for branch_spec in branch_dict.values():
                ends = getattr(branch_spec, "ends", None)
                if isinstance(ends, dict):
                    for dst in ends.values():
                        dst = str(dst)
                        if dst not in edge_map[src]:
                            edge_map[src].append(dst)

    # Legacy: _conditional_edges (older LangGraph versions)
    cond_edges = getattr(graph, "_conditional_edges", None) or {}
    for src, edge_spec in cond_edges.items():
        src = str(src)
        edge_map.setdefault(src, [])
        ends = getattr(edge_spec, "ends", None)
        if isinstance(ends, dict):
            for dst in ends.values():
                dst = str(dst)
                if dst not in edge_map[src]:
                    edge_map[src].append(dst)
        elif isinstance(edge_spec, dict):
            for dst in edge_spec.values():
                dst = str(dst)
                if dst not in edge_map[src]:
                    edge_map[src].append(dst)

    return edge_map


def extract_conditional_sources(graph: Any) -> set[str]:
    """Return node names that have conditional (branching) outgoing edges."""
    sources: set[str] = set()
    for src in getattr(graph, "branches", {}) or {}:
        sources.add(str(src))
    for src in getattr(graph, "_conditional_edges", {}) or {}:
        sources.add(str(src))
    return sources
