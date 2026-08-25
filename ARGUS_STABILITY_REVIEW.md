# ARGUS stability review — 50-agent live test bed

Source data: `~/argus-agents-lab` — 50 distinct LangGraph agents (≈150 nodes), each wired
with `ArgusWatcher().attach()`. Tested against `argus-agents==0.10.1`, Python 3.11,
macOS. Two proof modes per agent: offline (scripted fake LLMs) and live (real
`gpt-4o-mini`). Final state: **50/50 clean in both modes** (`check_suite.py` exit 0;
`run_live.py` exit 0, ~4.5 min/pass).

This document is written from the perspective of a consumer building a large, repeatable
test surface on ARGUS: what held up, what was brittle, what we'd need to call ARGUS
stable for fleet-scale use.

## What already works well

- **Refusal detection is the killer feature.** The first live pass caught an LLM node that
  replied *"It seems like your message is incomplete…"* to a bare-word prompt. Offline
  scripted fakes had masked it. ARGUS flagged the step `semantic_fail`, traced root cause
  to the right node (`draft_sections` ← upstream of crash site). Exactly the silent-failure
  class ARGUS advertises — confirmed in practice.
- **Root-cause chains point at origins, not crash sites** (`first_failure_step` +
  `root_cause_chain` were correct every time we checked).
- **Loop bookkeeping**: capped loops show healthy iterations as `retried` without poisoning
  overall status. Cyclic graphs persist fine.
- **Run persistence**: every `invoke()` wrote `.argus/runs/<ts>-<id>.json`; `total_tokens`
  / `total_cost_usd` recorded per run — enough to build cost dashboards without extra
  instrumentation.
- **Heuristic-only mode** (no API key) never crashed or degraded the run path.

## Where ARGUS needs work to be stable

### P0 — blockers for automated/CI consumption

1. **No machine-readable verdict.** The only public signals are human CLI output and the
   raw run JSON. A CI gate needs `argus check <run-id> --format json` (or `--fail-on
   silent_failure`) with a meaningful exit code. Today every harness must hand-parse
   undocumented JSON internals.
2. **Undocumented status taxonomy.** We observed `overall_status ∈ {clean, silent_failure}`
   while step statuses included `semantic_fail` and `retried`. The mapping
   (when does semantic_fail escalate overall status? is `retried` guaranteed healthy?) is
   nowhere specified. Consumers can't write stable assertions against an unspecified
   vocabulary.
3. **Findings are buried.** There is no top-level `findings` summary in the run JSON —
   consumers must walk `steps[].semantic_check` / `anomaly_signals` / `validator_results`
   and know each shape. One normalized findings list per run would make third-party
   integrations trivial and version-tolerant.

### P1 — brittleness we hit repeatedly

4. **Signatures are over-eager on legitimate content, with no suppression mechanism.**
   Real cases from our 50 agents:
   - `NL-002` flagged a severity string `"none"` as a serialized null.
   - `RF-001` flagged legitimate markdown structure (`"- ["` ×3) and echoed commitments
     (`"I will"` ×3) as repetition.
   - `BA-005` warned on flat scalar outputs, pushing agents toward artificial nesting.
   The current incentive is to contort *content* to satisfy the detector — backwards.
   Need per-node or per-project signature config (`argus ignore NL-002 --node draft_hook`)
   persisted in `.argus/config`.
5. **No run tagging/attribution.** Running suites concurrently (or even sequentially over
   50 agents into one `.argus/runs/`) makes "which run belongs to which agent/commit?"
   manual labor. Need `ArgusWatcher(tags={"agent": "04_sentiment", "suite": "live"})` and
   tag filters in `show`/`ui`.
6. **`argus show last` is ambiguous under concurrency.** Last-by-mtime breaks when several
   watchers write near-simultaneously. Tie-break by explicit run id everywhere.

### P2 — would make the test bed dramatically more useful

7. **Regression mode across runs.** Our whole purpose is rerunning the same 50 agents
   against successive ARGUS builds. A native `argus diff <runA> <runB>` (status deltas,
   new/disappeared findings, latency drift) would replace our custom comparison scripts.
8. **Determinism signal for fake-model masking.** Suggest an optional check: identical
   node outputs across repeated invocations of the same graph+input hint at scripted/
   cached LLMs — the exact blind spot that let six broken prompts pass offline.
9. **Flaky-failure tracking.** With repeated passes, distinguish persistent failures from
   stochastic LLM ones (needs cross-run memory keyed by graph+node).
10. **Docs**: document the JSON schema (`schema_version` exists — publish what it covers),
    the status vocabulary, and validator/anomaly field shapes.

## Detection recall — fault-injection matrix (the number that matters most)

Tolerating 50 good agents says little; catching broken ones is the product. We injected
10 failure classes (one per defect type, all offline, all exiting 0 except the crash
case) and scored ARGUS 0.10.1 heuristic-only mode. Regenerate: `.venv/bin/python
faults/_run_matrix.py` → `faults/FAULT_MATRIX.md`.

**Recall: 4/10 caught** (overall_status != clean).

| caught | missed |
|---|---|
| dropped field (BA-006), crash root-cause (`crashed` + correct chain), placeholder output (PH-007/RF-005), validator breach | empty `{}` node update, swallowed type contract violation, loop stall, wasted retries, degraded/truncated text, implausibly fast LLM call |

Root causes of the six misses, verified against installed source:

1. **No-op node updates are invisible.** A node returning `{}` passes; nothing compares
   "fields this node's consumers need" vs "fields it set". Needs consumer-aware schema
   expectations.
2. **Swallowed exceptions look clean.** If application code catches its own error and
   falls back, every step reports pass. Consider flagging suspiciously broad fallbacks
   (constant outputs after exception-shaped control flow).
3. **Loop stall / wasted-retry analysis requires the hosted LLM proxy.**
   `loop_analyzer.py` errors with "Not logged in" without `argus login`, leaving
   `loop_analyses: []`. The README markets these as features; offline they silently
   no-op. Either ship a local heuristic fallback or say plainly they're cloud-only.
4. **Warnings don't escalate.** Degraded text earns only severity=warning
   (`shallow_output`), step stays `pass`, run stays `clean`. Define when warnings should
   aggregate into a failure (or expose a strictness knob).
5. **Latency checks are unreachable by default.** `suspiciously_fast` requires
   `min_expected_ms`; `ArgusConfig` has no default and `ArgusWatcher.__init__` doesn't
   accept it as a kwarg — the check cannot fire on default config at all today.
6. **PH signatures match whole fields only (`exact_ci`).** `"TBD"` embedded in prose
   never fires; fault 07 was caught only because sentinel fields were exactly `"TBD"`.
   Add substring/contains matching tiers for prose fields.

Also observed: BA-001/BA-005 anomaly ids appear even in fully clean runs as low-severity
noise — noise floor this high trains users to ignore anomaly ids entirely.


## Evidence appendix

| Observation | Run id | Detail |
|---|---|---|
| Refusal caught at origin | `20260825-034913-849fe0` | `draft_sections` got clarification-request instead of bullets; root cause chain correct |
| NL-002 false positive | batch B5 verification | `"none"` severity string read as serialized null |
| RF-001 false positives | B3/B5 verification | markdown bullet markers + echoed commitments counted as repetition |
| Semantic_fail vs overall mapping | `…-034931-5984d2`, `…-034957-6995e7` | steps `semantic_fail` → overall only `silent_failure` |
| Concurrent-writer ambiguity | all swarm batches | per-agent attribution required reading `graph_node_names` out of each JSON |

Reproduce any of this: see `~/argus-agents-lab/README.md` (build + rerun guide).
