# Dropped ideas

Proposals considered and **recommended against** — either already built, or a poor fit for what
ARGUS is. Recorded here so they stop resurfacing.

---

## 1. "Build a Root-Cause Attribution Engine"

**Verdict: already shipped. Do not rebuild.**

The proposal: when a multi-agent system delivers a broken answer, trace backward through state
history, find where quality dropped most, and report something like *"84% probability the failure
originated in Researcher Agent at Step 3."*

This is `correlator.py` — 805 lines, already in the product and already wired into every run.

| Proposed capability | Where it already lives |
|---|---|
| Trace backward from a failure | `correlator.correlate(record)`, called from `session.finalize()` |
| Identify the origin node | `DegradationOrigin(node_name, step_index, signal_types, confidence, reason)` |
| Show how it spread | `PropagationChain` + `PropagationLink(source, target, signal_type, confidence, evidence)` |
| Confidence score | `confidence: float` on both origins and links |
| Clean summary instead of a log dump | `CorrelationReport.causal_summary`, a 1–3 sentence narrative |
| Timeline of degradation | `TimelineEvent` with `degradation_onset` / `propagation` / `crash` markers |
| LLM-augmented explanation | `llm_correlator.py` → `LLMCorrelationInsight` |

The README has advertised this since before the proposal was written: *"Correlator — traces failure
propagation across nodes. Points at the origin, not the crash site."*

### The one genuine upgrade

The proposal's distinctive detail — scoring **semantic drift of the state vector at every node
transition** — is not implemented. The correlator reasons over discrete signals (dropped fields,
tool failures, placeholder matches), not over continuous embedding distance between consecutive
states.

That is worth building, and it is cheap: `embedding_store.py` already provides
`text-embedding-3-small` with a SQLite cache and cosine similarity. Embedding consecutive state
snapshots and flagging the largest drop would add a genuinely new signal to
`DegradationOrigin`.

**But log it as an enhancement to `correlator.py`, not as a new feature.** The difference in scope
is roughly a quarter versus an afternoon, and framing it as greenfield work is how a team ends up
building a second correlator next to the first one.

---

## 2. RAG chunk-to-question generator (synthetic evaluation data)

**Verdict: good idea, wrong product.**

The proposal: generate synthetic test questions from RAG chunks at three difficulty tiers — direct,
messy/typo-laden, and questions that implicitly contradict the chunk to test whether the agent
catches the nuance or hallucinates.

The critique attached to it is correct: naive chunk-to-question generation produces questions that
match the source text too closely, so the agent passes the test and fails on real users. The
"adversarial humanization" fix is a genuinely sharp insight.

### Why it does not belong in ARGUS

ARGUS is a **forensic recorder**. Every module in `src/argus/` operates on something that already
happened: `session.py` captures execution, `storage.py` persists it, `correlator.py` explains it,
`replay.py` re-runs it. The product's whole claim is fidelity about the past.

A question generator produces *inputs that never existed*. It shares no data model with anything
here — no `RunRecord`, no `NodeEvent`, no graph topology. It would be a separate codebase living
in the same repo, sold to a different buyer (a RAG engineer building a dataset, not a platform
engineer debugging a pipeline), competing with Ragas and DeepEval on their home ground.

The strategic cost is worse than the engineering cost: ARGUS's differentiation is being the tool
that catches silent failures in agent graphs. Adding a synthetic-data generator dilutes that into
"another LLM eval platform", which is the crowded category the product is deliberately not in.

### What to do instead

If synthetic scenarios are wanted, the ARGUS-shaped version is to **derive them from recorded
runs** rather than from source documents — take real recorded states and mutate them. That fits
the existing data model exactly, and the primitive already exists: `state_patch.py` mutates a
recorded state, and `session.frozen_outputs` substitutes tool responses. See
[indirect-injection-testing.md](indirect-injection-testing.md), which uses precisely this approach
for adversarial scenarios.

Generating questions from chunks is a good product. It is someone else's.
