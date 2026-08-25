# Live loop guard (loop detonator)

**Status:** half built · **Verdict:** real delta, but handle with care · **Size:** small–medium

## The idea

Agents caught in an existential loop (A asks B, B asks A) burn thousands of dollars in tokens.
Rather than a crude `max_iterations = 10`, analyze *state trajectory*: if the similarity of the
last three state transitions exceeds ~0.95, halt execution, alert the pipeline, and show a loop
visualization.

## What already exists

More than the pitch assumes.

- **Loop analysis is already shipped** — `loop_analyzer.py` runs mandatory LLM analysis on every
  looped node, producing `LoopAnalysisResult` with `is_stalled`, `stall_details`,
  `unnecessary_retries`, and per-iteration diffs. The README documents "Loop stalls" and
  "Unnecessary retries" as things ARGUS already catches.
- **Iteration tracking is live during the run.** `session.py` maintains `attempt_index` per node
  event and sets `total_iterations` on finalize. The counter the guard needs is already there.
- **Cyclic graphs are detected up front** — `utils/cycle_detection.py`, `RunRecord.is_cyclic`.
- **Embedding + cosine similarity infrastructure exists.** `embedding_store.py` wraps
  `text-embedding-3-small` with a SQLite cache, batch computation, and similarity — the exact
  primitive the "0.95 similarity" proposal needs. `tests/test_semantic_similarity.py` covers it.

## What's genuinely missing

**Timing.** `loop_analyzer.py` runs *post-hoc*, at finalize, after the tokens are already spent.
It tells you that you burned $40 in a loop. It does not stop you burning it.

The delta is a guard inside the wrapper — `session.py`, `_make_sync_wrapper` /
`_make_async_wrapper`, where `attempt_index` is already tracked. On each iteration past a
threshold, embed the state delta and compare against the previous N iterations. On stagnation,
act.

Also missing: the loop visualization map.

## Why it's parked

Because "act" is a dangerous verb, and the pitch's framing — *halt execution* — is the wrong
default.

ARGUS's whole promise is that it is a passive observer you can wrap around a pipeline without
changing its behavior. A monitoring tool that kills a production run because a similarity heuristic
crossed 0.95 will eventually kill a legitimate one — a retry loop that genuinely needs six
near-identical attempts before converging, for instance. The first time that happens in
production, ARGUS gets removed from the stack.

Recommended design if built:

1. **Default to warn.** Log loudly, record a signal, do not interrupt. Halting is opt-in.
2. **Cost-aware, not just similarity-aware.** `llm_tracker.py` and `pricing.py` already compute
   per-call cost. "You have spent $12 on a loop showing no state progress" is more actionable, and
   far safer, than a bare similarity number.
3. **Embedding calls cost money too.** Embedding every iteration of every looped node adds spend to
   a feature whose purpose is reducing spend. Gate it behind an iteration threshold and reuse the
   `embedding_store.py` cache.
4. **Separate the two audiences.** In CI, halting is correct and cheap. In production, warning is
   correct. The config should make that distinction explicit rather than picking one.
