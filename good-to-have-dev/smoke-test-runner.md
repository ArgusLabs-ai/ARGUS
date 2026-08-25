# Smoke test runner (graph-aware pass/fail)

**Status:** not built · **Verdict:** gateway feature · **Size:** large

## The idea

Run a dataset of scenarios against a pipeline and report pass/fail — but *graph-aware*. Not
"80% passed", but: "10 scenarios failed; in 90% of them the agent took the Refund route instead
of Escalation. The issue is localized to Node X."

Linking test results back to graph topology is the differentiator; plain pass/fail percentages are
a commodity (Promptfoo, DeepEval).

## What already exists

- **Per-run pass/fail already exists at node granularity.** `NodeEvent.status` is one of
  `pass | fail | crashed | degraded_input | semantic_fail | interrupted | retried`, and
  `RunRecord.overall_status` aggregates it.
- **Path data is already recorded.** `RunRecord.steps` in execution order plus
  `graph_edge_map` means the actual route through the graph is fully reconstructable per run —
  this is precisely the data the "which route did it take" analysis needs, and it is already
  persisted.
- **Failure localization already exists.** `correlator.py` produces `DegradationOrigin` with a
  confidence score and `PropagationChain`. The "localized to Node X" claim is largely a
  cross-run aggregation of something ARGUS already computes per run.
- **Cross-run comparison exists** for pairs — `cmd_diff.py`, `ReplayComparisonResult`.
- **Run listing and storage** — `storage.list_runs`, `build_replay_tree`.

## What's genuinely missing

1. **A dataset concept.** No notion of a scenario set. Needs a format, a loader, and a place to
   live in `.argus/`.
2. **A runner.** Execute N scenarios against a pipeline, ideally in parallel. The natural shape is
   `asyncio` — `session.wrap()` already handles async node functions via `_make_async_wrapper`,
   so async pipelines are supported; the missing piece is concurrency *across* runs, and the
   concern there is that `ArgusSession` state and the `.argus/runs/` writes must stay isolated
   per scenario.
3. **Cross-run aggregation.** Everything today is per-run or pairwise. Route-frequency analysis
   ("90% of failures took this path") needs an N-run aggregation layer that does not exist.
4. **CI surface.** A non-zero exit and a machine-readable report.

## Why it's parked

Size, and ordering. This is the largest item in this folder, and two of its dependencies are
cheaper to build first:

- It wants a **baseline** to compare against — that is the golden-trace tag from
  [regression-contracts.md](regression-contracts.md).
- It is the **prerequisite** for the drift metrics ("tool utilization dropped 14%") and for
  [indirect-injection-testing.md](indirect-injection-testing.md), which needs a harness to run
  many adversarial scenarios.

Build golden traces first, then this, then injection testing on top.
