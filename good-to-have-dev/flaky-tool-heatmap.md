# Flaky tool & API heatmap

**Status:** not built · **Verdict:** cheap, data already captured · **Size:** small–medium

## The idea

Traditional APM shows how long a tool took. But agents fail because tools are flaky, rate-limited,
or return unstructured garbage that breaks the parser. The interesting failure is the one where
the API returns `200 OK` and the agent's JSON parser fails to read it 40% of the time.

Track tool-to-LLM alignment and produce a heatmap of which tool schemas need rewriting.

## What already exists

The data collection is essentially done; the aggregation is not.

- **Every HTTP call is already captured.** `http_recorder.py` records method, URL, request body
  hash, and response for every outbound call, saved to `.argus/runs/<run-id>.http.json`. Status
  codes and bodies are already on disk.
- **Tool failures are already modelled.** `ToolFailure` (`models.py`) classifies
  `error_response | rate_limit | empty_result | error_in_data | partial_failure` with severity and
  evidence, attached to `InspectionResult.tool_failures`.
- **Per-node timing and token cost** — `NodeEvent.duration_ms`, `llm_tracker.py`, `pricing.py`.
- **Signature hit statistics across runs already exist.** `signature_stats.py` tracks hit metadata
  and prunes stale signatures — an existing precedent for cross-run aggregation to follow.
- **Anomaly detection** (`anomaly_detector.py`) already flags output-size and timing outliers.

## What's genuinely missing

1. **Cross-run aggregation of tool outcomes.** Everything above is per-run. The heatmap is
   fundamentally "group by tool across the last N runs", and there is no such layer.
2. **The 200-OK-but-unparseable correlation.** This is the actually novel bit: joining an HTTP
   response that succeeded to a *downstream node failure* that followed it. The join key exists
   (both are within a run, ordered by step) but nothing computes it.
3. **A tool identity.** HTTP interactions are keyed by URL, node events by node name. Attributing
   a call to a named tool requires a mapping that does not exist — the pragmatic first version
   groups by URL host plus path prefix.
4. **A visualization.** Heatmap in the dashboard.

## Why it's parked

Not blocked on anything — this is buildable today and is a good candidate right after the graph
linter. It is parked mainly because (3) needs a design decision about what a "tool" is in ARGUS's
model, and getting that wrong makes the aggregation meaningless.

Worth noting the strategic value: it is the one idea in this folder that produces insight from
data ARGUS is *already collecting and currently throwing away* at the end of each run. The cost is
aggregation, not instrumentation.
