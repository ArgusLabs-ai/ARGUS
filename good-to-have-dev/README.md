# Good to have — dev

Parked feature ideas for ARGUS, with an honest assessment of each.

This folder exists because a long brainstorm produced a list of "ARGUS should build X" ideas, and
a surprising number of them turned out to be **already built**. Rather than let that analysis
evaporate into a chat log and get re-litigated in three months, each idea gets a short decision
record here.

## What each doc contains

1. **The idea** — what was proposed, stated fairly
2. **What already exists** — the specific modules in `src/argus/` that already do part of it
3. **What's genuinely missing** — the real delta
4. **Rough size** — honest effort estimate
5. **Why it's parked** — what would need to be true to build it

That third section is the point. Almost every proposal here is partially built already, and the
difference between "build a blame attribution engine" and "add embedding-based drift scoring to
`correlator.py`" is the difference between a quarter and an afternoon.

## Current state

| Idea | Status | Verdict |
|---|---|---|
| [Regression contracts](regression-contracts.md) | Not built | **Build next.** Needs a golden-trace tag first |
| [Graph hazard linter](graph-hazard-linter.md) | Not built | **Cheapest standalone win.** No LLM, no deps |
| [Live loop guard](live-loop-guard.md) | Not built | Iteration data exists; analysis and intervention are the delta |
| [Smoke test runner](smoke-test-runner.md) | Not built | Gateway to graph-aware pass/fail and drift metrics |
| [Indirect injection testing](indirect-injection-testing.md) | Not built | Strongest differentiator; primitive already exists |
| [Flaky tool heatmap](flaky-tool-heatmap.md) | Not built | Data is already captured, just not aggregated |
| [Dropped ideas](dropped-ideas.md) | — | Two proposals recommended **against** building |

## Already shipped — do not re-propose

These came up as "ARGUS should build this" and already exist:

| Proposal | Where it lives |
|---|---|
| Resume execution from step N | `replay.py` — `ReplayEngine.replay()`, upstream outputs frozen |
| Time-travel state patching | `state_patch.py` + `argus replay --set/--delete/--patch` |
| Root-cause / blame attribution | `correlator.py` — `DegradationOrigin` with confidence scores |
| Behavioral diff between runs | `cmd_diff.py` + `ReplayComparisonResult` |
| Deterministic replay of external calls | `http_recorder.py` — record/playback |
| Custom per-node assertions | `validators={...}` on `ArgusWatcher` / `ArgusSession` |
| Full step-by-step state capture | `NodeEvent.input_state` / `.output_dict` |

## Promoting an idea out of this folder

When one gets built: delete its doc in the implementing PR and link the PR from the table above.
The folder should shrink over time. If a doc has been here a year untouched, that is information —
either it is not actually important, or the "what's missing" section is wrong.
