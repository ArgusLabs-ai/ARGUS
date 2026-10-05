# Research Agent Demo Pipeline — Design

**Date:** 2026-09-03
**Status:** approved
**Goal:** A realistic LangGraph research agent that exercises the ARGUS dashboard with
failures that genuinely occur, rather than hardcoded degraded outputs.

## Motivation

`demo/coherence_demo.py` fabricates its failures — `lambda s: {"recommendation":
"bearish sell"}` exists only to trip Rule 15. It proves the rule fires; it proves
nothing about whether ARGUS catches real problems. This pipeline replaces that with
real network calls and real model output.

The existing 2,994 runs in `.argus/runs/` are almost all test fixtures (`A→B→C`,
single-node), so the dashboard has never been driven by a realistic graph.

## Principle: induced conditions, real failures

The distinction this design turns on:

- **Fabricated** — `return {"summary": "PLACEHOLDER"}`. Never done here.
- **Induced but authentic** — give a summariser `max_tokens=40` against a 2,000-word
  source. The model genuinely truncates mid-sentence. The *condition* is chosen; the
  *failure* is real model behaviour.

Node functions contain no scenario awareness. There is no `if scenario == "bad"`
branch anywhere. Scenarios are configuration that changes the conditions nodes run
under — a different source list, a different token budget, a different prompt.

## Architecture

```
ingest_query → plan_subqueries(LLM) → fetch_sources(HTTP)
                                          ↓ fan-out
                        summarize_a │ summarize_b │ summarize_c   (LLM, parallel)
                                          ↓ join
                                   merge_summaries
                                          ↓
                                   synthesize_report(LLM)
                                          ↓
                                   verify_citations(LLM) ──pass──→ finalize
                                          ↑                │
                                          └──── revise_report ←┘  (cycle, max 2)
```

11 nodes: linear head, three-way parallel fan-out, a join, conditional routing, and a
bounded retry cycle. Chosen so the execution graph, root-cause chain and cycle
detection all have something real to display.

## Modules

| File | Responsibility |
|---|---|
| `demo/research_agent/nodes.py` | Node functions. Plain Python; no scenario awareness. |
| `demo/research_agent/scenarios.py` | The five condition sets, each documenting the real-world situation it mirrors. |
| `demo/research_agent/pipeline.py` | `StateGraph` wiring + `ArgusWatcher` attachment. |
| `demo/research_agent/run_demo.py` | CLI: `--scenario clean\|dead_source\|field_drop\|starved_context\|bad_citations\|all`. |
| `demo/research_agent/README.md` | What each run demonstrates. |

Not committed — `demo/research_agent/` is in `.gitignore` (line 87). The pipeline uses
a real API key and makes real network calls.

## Scenarios

| Scenario | Induced condition | Real failure produced | ARGUS should record |
|---|---|---|---|
| `clean` | none | none | clean run; `argus check` exits 0 |
| `dead_source` | source list contains a missing Wikipedia page and a `.invalid` domain | genuine HTTP 404; genuine DNS resolution failure | `error_response`, `empty_result` |
| `field_drop` | `merge_summaries` has a real wrong-key bug in a dict comprehension | comprehension matches nothing → returns `{}` → `synthesize_report` raises a real `KeyError` | `empty_output` (critical); crash; root-cause chain blames `merge_summaries`, not the crash site |
| `starved_context` | `max_tokens=40` against ~2,000-word sources | model genuinely truncates mid-sentence | `truncated_output`; semantic judge |
| `bad_citations` | synthesise prompt omits source IDs | model genuinely invents references; verifier genuinely rejects them | real retry cycle; `retried`; iteration counts |

Each induced condition carries a comment naming what it mirrors in production: a
link-rotted source, a copy-paste key bug, a tight token budget, a prompt missing
grounding.

## External dependencies

- **Sources:** Wikipedia REST API (`/api/rest_v1/page/summary/...`), no auth. Verified
  reachable: real 200 with content, real 404 for a missing page, real DNS failure
  (curl exit 6) for a `.invalid` domain.
- **Model:** `gpt-4o-mini` via `OPENAI_API_KEY` from `.env`.

**Critical:** the shell has `OPENAI_API_KEY` set to an empty string, and
`load_dotenv()` will not overwrite an existing variable. The demo must call
`load_dotenv(override=True)` or every model call fails with missing credentials.

**Cost:** ~15–20 `gpt-4o-mini` calls for a full sweep; under $0.05.

## Error handling

Nodes let real errors propagate — that is the point. `fetch_sources` catches network
errors per-source and records the failure in state rather than aborting, mirroring how
a real agent degrades when one source dies. `synthesize_report` does **not** guard
against the missing key: the `KeyError` is the observable failure ARGUS must trace.

## Verification

After the sweep, for each run:

1. `argus check <run-id>` — exit code matches the scenario's intent (0 only for `clean`).
2. Recorded findings match what actually happened: `dead_source` shows a real HTTP
   failure; `field_drop` blames `merge_summaries` rather than `synthesize_report`.
3. The run appears in `argus ui` with the expected status chip.

If ARGUS mis-attributes a failure, that is a genuine ARGUS bug and worth reporting
separately — the demo doubles as an end-to-end check of the detection pipeline.

## Out of scope

- Committing the demo to the repo (explicitly excluded).
- Replacing or deleting `demo/coherence_demo.py`.
- Any change to ARGUS detection logic. If detection is wrong, it gets reported, not patched here.
