<div align="center">
  <img src="https://github.com/VaradDurge/ARGUS/blob/master/assets/Argus-NameTrans.png?raw=true" width="480"/><br/>
  <a href="https://arguslabs.in"><img src="https://img.shields.io/badge/website-arguslabs.in-6366f1" alt="Website"/></a>
  <a href="https://pypi.org/project/argus-agents/"><img src="https://img.shields.io/pypi/v/argus-agents" alt="PyPI version"/></a>
  <a href="https://pypi.org/project/argus-agents/"><img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+"/></a>
  <a href="https://github.com/VaradDurge/ARGUS/releases"><img src="https://img.shields.io/badge/status-beta-6366f1" alt="Beta"/></a>
</div>

---

**Catch silent failures in AI agent pipelines before production.**

Your LangGraph pipeline runs fine — no exception. But three nodes later, something crashes with a `KeyError`. The real cause? A node upstream silently dropped a field. ARGUS catches this.

Beta, and under active development. ARGUS is early. Expect rough edges and bugs, and expect things to move. Issues and pull requests are welcome.

---

## How to use ARGUS

**1. Install**

```bash
pip install argus-agents
```

**2. Init**

```bash
argus init
```

Writes `.cursor/skills/argus-debug/` and `.claude/skills/argus-debug/`. Commit them. The skill already contains the setup prompt.

**3. Attach**

Ask your editor agent to wire ARGUS. (The skill already contains this AI setup prompt; the landing-page copy is just a fallback.)

```python
from argus import ArgusWatcher
app = ArgusWatcher().attach(graph)
```

<img src="https://github.com/VaradDurge/ARGUS/blob/master/assets/Argus%20Guidelines%20and%20Contribution.png?raw=true" width="700"/>

**4. Run**

Same as always. Failures print in the terminal; clean runs stay silent.

```
[argus] run 8f3a1c02  silent_failure on retrieve
        missing: documents  (dropped by search)
        argus show last   |  argus ui
```

**5. Inspect**

```bash
argus show last
argus fix <id>     # paste-ready prompt for the root-cause node
argus ui
```

Empty dashboard → wrong directory or no run yet. Check project root or `$ARGUS_DIR`.

**Optional** — `argus key set` for the LLM judge. Skip it and you still get heuristics.

## Bring Your Own Key (BYOK)

AI-powered detection (the semantic judge, LLM investigator, learned trends) uses **your own** key from the provider of your choice — **OpenAI**, **Anthropic** (Claude), or **Google** (Gemini). Set it once and it's saved locally for every future session:

```bash
argus key set                          # OpenAI by default — prompts, hidden input
argus key set --provider anthropic     # or Anthropic (Claude)
argus key set --provider google        # or Google (Gemini)
# pass it directly instead of being prompted:
argus key set sk-... --provider openai
# or just export it (env wins over the saved key):
export OPENAI_API_KEY=sk-...           # or ANTHROPIC_API_KEY / GEMINI_API_KEY
```

Configured more than one? Switch the active provider anytime:

```bash
argus key use anthropic  # activate a provider you already have a key for
argus key show           # list configured providers (masked); * marks the active one
argus doctor             # reports BYOK provider / hosted / heuristic-only mode
```

You pick the **provider**; ARGUS picks a sensible balanced model for each internal call (a cheap model for the frequent per-node checks, a stronger one for root-cause reasoning). Per-provider resolution order: env var (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`) → saved key → (hosted proxy, if you're on the cloud tier) → heuristic-only.

No key? ARGUS still works — it falls back to heuristic-only detection, no crashes.

Hosted cloud sync (`argus login`) is optional and only applies if a hosted backend is configured.

---

## Quick Start (manual)

```python
from argus import ArgusWatcher

watcher = ArgusWatcher()
app = watcher.attach(graph)         # StateGraph or already-compiled app
result = app.invoke(initial_state)  # run is persisted automatically
```

ARGUS monitors every node, detects failures, and saves the run. No changes to your node functions.

> **`finalize()` is optional.** `attach()` wraps `invoke()` / `ainvoke()` / `batch()` / `abatch()` / `stream()` so the run is written to `.argus/runs/` when the outermost call returns — including cyclic graphs. Calling `watcher.finalize()` afterwards is a no-op.

Constructor form still works if you compile yourself:

```python
watcher = ArgusWatcher(graph)       # uncompiled StateGraph
app = graph.compile()
result = app.invoke(initial_state)
```
---

## What It Catches

| Problem | Example |
|---------|---------|
| **Silent failures** | Node returns `{}` or drops a required field — no exception, pipeline keeps running broken |
| **Semantic failures** | Output structure is fine but values are wrong (placeholders, refusals, degraded text) |
| **Loop stalls** | Agent retries 5 times producing identical output — stuck loop burning tokens |
| **Unnecessary retries** | Loop produces correct answer on attempt 2, but validator forces 3 more iterations |
| **Crash root cause** | Traces `KeyError` at node 5 back to the upstream node that actually dropped the field |
| **Contract violations** | Output types don't match the next node's expected input schema |
| **Latency degradation** | Node takes 95%+ of timeout, or suspiciously fast LLM call (likely cached/empty) |
| **Conditional path confusion** | Unchosen branches correctly shown as "skipped" — not false "crashed" |

---

## Detection Layers

Runs in order, each more expensive — only fires when needed. Every status a layer can assign, and how node statuses roll up into the run verdict, is specified in [`docs/STATUS.md`](docs/STATUS.md).

1. **Heuristics** — 150+ failure signatures (placeholders, empty results, error keys, semantic degradation). Zero cost.
2. **Validators** — custom per-node business-logic constraints. Deterministic.
3. **Anomaly detector** — statistical checks for output size anomalies, timing outliers. Deterministic.
4. **Correlator** — traces failure propagation across nodes. Points at the *origin*, not the crash site.
5. **LLM semantic judge** — evidence-aware final ruling. Receives all signals from layers 1–4 before deciding. Cannot override validator failures or critical anomalies.
6. **LLM investigator** — root cause explanations and debugging suggestions. Only on ambiguous failures.
7. **Loop analyzer** — LLM analysis for looped nodes: summarizes iterations, detects stalls, flags wasted retries.

---

## Loop-Aware Inspection

Pipelines with loops (LLM -> compiler -> if fail, retry) get special treatment:

- Earlier iterations that self-corrected are marked `retried` (not counted as failures)
- Only the **final iteration** determines pass/fail
- LLM analyzes every loop: what went wrong, what changed between attempts, whether retries were necessary
- Dashboard shows iteration badges, collapse/expand, and natural-language loop summaries

---

## Replay

Fix a bug, re-run from the failing node. Skip upstream nodes entirely:

```bash
argus replay <run-id> node_7          # re-run from node_7 onward
argus replay <run-id> node_7 --only   # just that one node
argus diff <rerun-id>                 # compare vs original
```

External API calls (OpenAI, etc.) are recorded by default — replays are free and deterministic.

### Time-Travel: edit the state, then resume

Spotted the bad value? Fix it in the saved state and resume from there — no code change, no re-running the steps that already worked:

```bash
argus replay <run-id> node_7 --set status=OK          # correct a value
argus replay <run-id> node_7 --delete stale_field     # reproduce a dropped field
argus replay <run-id> node_7 --patch fix.json         # a full patch document
argus replay <run-id> node_7 --set status=OK --dry-run  # preview, run nothing
```

Upstream nodes stay frozen, so only the resumed trajectory changes. Paths are dotted with list
indices — `items[0].name` — and match the `field_path` ARGUS reports on a failing signal, so you
can paste one straight in. A patch file takes the same three ops:

```json
{
  "delete": ["broken_field"],
  "set":    {"query": "fixed query", "meta.retries": 0},
  "merge":  {"config": {"temperature": 0}}
}
```

Patches are strict by default: a mistyped path errors with a "did you mean" hint instead of
silently adding a field (use `--create-missing` to add new keys). Every patched replay records
the patch it ran with, so the run explains its own divergence from the original.

---

## Semantic Judge

For subtle quality issues that pattern matching can't catch:

```python
watcher = ArgusWatcher(graph, semantic_judge=True)  # opt-in; default is off
```

LLM evaluates output quality on every node. Catches wrong tone, unhelpful responses, outdated info. Requires a provider key (OpenAI, Anthropic, or Google) — set via `argus key set [--provider ...]` (see [BYOK](#bring-your-own-key-byok)).

The judge receives **all prior evidence** — validator failures, anomaly signals, inspection results — so it rules with full context, not just input/output. Every decision includes an audit trail:

```json
{
  "pass": false,
  "reason": "Validator correctly identified missing resolution_ticket",
  "confidence": 0.85,
  "evidence_considered": ["validator:payment_check", "anomaly:BA-003"],
  "overridden_signals": []
}
```

- `evidence_considered` — which prior signals the LLM weighed
- `overridden_signals` — which signals the LLM disagreed with (passed despite the flag)

---

## Custom Validators

```python
watcher = ArgusWatcher(graph, validators={
    "classify": lambda o: (o.get("label") in ["yes", "no"], "unexpected label"),
    "*":        lambda o: ("error" not in o, "error key present"),  # runs on every node
})
```

Validator failures cannot be overridden by the LLM judge — they are hard constraints.

---

## Configuration

```python
from argus import ArgusWatcher, ArgusConfig

config = ArgusConfig(
    semantic_judge=True,           # LLM judge on every node (default: False)
    judge_model="gpt-4o",          # model for the judge
    node_timeout_ms=30000,         # flag outputs at ≥95% of this
    min_expected_ms=500,           # flag suspiciously fast LLM nodes
    sample_rate=0.5,               # persist 50% of clean runs (save disk)
    persist_failures=True,         # always persist failed runs
)

watcher = ArgusWatcher(graph, config=config)
```

---

## CLI

```
argus list                           # all recorded runs
argus show last                      # most recent run
argus show <id>                      # inspect a specific run
argus check last                     # CI gate — exit 1 on crash / silent failure / semantic fail
argus check <id>                     # same gate for a specific run
argus check last --format json       # same verdict as one JSON object (run_id, overall_status, passed, findings[])
argus check last --fail-on crashed,silent_failure   # only these run statuses fail the gate
argus inspect <id> --step <node>     # dump raw input/output for a node
argus fix <id>                       # fix prompt for the root cause, ready to paste
argus replay <id> <node>             # re-run from a node
argus diff <id-a> <id-b>             # compare two runs
argus stats                          # signature hit stats, disable/enable/dispute signatures
argus ui                             # web dashboard
argus doctor                         # check setup health + LLM mode (BYOK/hosted/heuristic)
argus key set [--provider ...]       # save a provider key locally (OpenAI/Anthropic/Google) — BYOK
argus key use <provider>             # switch the active provider
argus key show                       # list configured providers (masked); * marks active
argus key clear [--provider ...]     # remove one provider's key, or all
argus login                          # (optional) sign in for hosted cloud sync
argus logout                         # clear stored credentials
argus whoami                         # show current login status
argus update                         # check for newer release
```

---

## pytest plugin

Silent failures become test failures without changing how you invoke the graph:

```bash
pytest --argus
```

ARGUS auto-wraps `StateGraph.compile()` / compiled `invoke()` for the test session. A clean pipeline stays a passing test; missing fields, tool failures, crashes, and semantic degradation fail that test. Tests that never invoke a graph are unchanged. Pair with `argus check last` in CI after a standalone run.

---

## Web Dashboard

```bash
argus ui    # opens at localhost:7842
```

Shows all runs, node-level detail, AI analysis, replay diffs, loop iteration badges, and comparison views. No account needed for local use.

If the table is empty, the UI is serving a different `.argus` than the project that just ran, or there are no runs yet. The empty state shows the path ARGUS is reading and what to do (`argus show last`, run the graph, check cwd vs project root).

- **Distinct failure colors** — crashed (red), silent failure (amber), semantic fail (purple), degraded input (orange), skipped (gray)
- **Evidence audit trail** — see exactly which signals the LLM judge considered and which it overrode
- **Side-by-side diff** — compare any two runs node-by-node

---

## Without LangGraph

```python
from argus import ArgusSession

session = ArgusSession()
session.set_edges({"fetch": ["classify"], "classify": ["process"]})

fetch    = session.wrap("fetch",    fetch_fn)
classify = session.wrap("classify", classify_fn)
process  = session.wrap("process",  process_fn)

state = fetch(initial_state)
state = classify(state)
state = process(state)
session.finalize()
```

Works with any framework — Prefect, Temporal, plain Python.

---

## Requirements

- Python 3.9+
- LangGraph 0.2+ (only for `ArgusWatcher`)
- A provider key (OpenAI, Anthropic, or Google) for semantic features — set via `argus key set [--provider ...]` (optional; all heuristic detection works without it)

For AI setup prompts and integration guides, visit **[arguslabs.in](https://arguslabs.in)**.

---

**v0.8.12** — [changelog](https://github.com/VaradDurge/ARGUS/releases)

## License

ARGUS is **open-core**. The open-source core (`src/argus/`, the `argus-agents` PyPI
package) is licensed under **Apache-2.0** — see [LICENSE](LICENSE). The `cloud/`
directory (hosted/enterprise components) is proprietary — see [cloud/LICENSE](cloud/LICENSE).
