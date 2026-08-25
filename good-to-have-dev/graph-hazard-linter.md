# Graph hazard linter (static analysis for agent graphs)

**Status:** not built · **Verdict:** cheapest standalone win · **Size:** small (~2–4 days)

## The idea

SAST, but for graph topology. Before a single token is spent, scan the graph structure and flag
architectural hazards:

- **Dead ends** — nodes that consume state but never pass it forward
- **Unbounded cycles** — loops with no explicit exit condition
- **State collisions** — parallel nodes writing the same state key
- **Unreachable nodes** — present in the graph, never on any path from entry

A linter for multi-agent systems, catching structural failures before runtime.

## What already exists

- **The topology is already extracted.** `patcher.extract_edge_map(graph)` builds
  `{source: [dest]}` and already handles the hard cases: standard edges, LangGraph ≥0.2
  `branches` with `BranchSpec.ends`, and legacy `_conditional_edges`. This is the input the
  linter needs and it is done.
- **Cycle detection exists.** `utils/cycle_detection.py`, used to set `RunRecord.is_cyclic` and to
  warn when a cyclic graph is garbage-collected without `finalize()` (`watcher.py:102`).
- **The edge map is persisted per run.** `RunRecord.graph_edge_map`, so hazards can be reported
  against a recorded run as well as a live graph.
- **Node type hints are already introspected.** `utils/type_introspection.py` and
  `inspector.py` read successor annotations — the same machinery that would detect state-key
  collisions between parallel branches.

## What's genuinely missing

The analysis pass itself. Given `graph_edge_map`, each check is a straightforward graph algorithm:

- Dead end → node with no outgoing edges that is not a designated terminal
- Unreachable → not in the transitive closure from the entry node
- Unbounded cycle → strongly-connected component with no edge leaving it toward a terminal
- State collision → needs write-key inference per node, the only genuinely hard one; the honest
  first version reads type hints and return annotations and skips nodes it cannot analyze

Plus a CLI surface (`argus lint`) and a non-zero exit for CI.

## Why it's parked

Not because it is hard — it is the cheapest thing on this list, needs no LLM, no new dependencies,
and no changes to any existing code path. It is parked because it is **orthogonal to ARGUS's core
loop**: everything else in the product reasons about recorded runs, and this reasons about a graph
that has not run yet.

That makes it an excellent standalone contribution, and a good first task for someone new to the
codebase. It is genuinely differentiated — no competitor lints graph topology.

Caveat worth designing around: static analysis of a dynamic graph will produce false positives
(conditional edges resolved at runtime, nodes added programmatically). Ship it as advisory output
first, and only add a CI-failing mode once the false-positive rate is known.
