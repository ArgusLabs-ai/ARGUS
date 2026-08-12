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

---

## Install

```bash
pip install "argus-agents[all]"
```

This gets the full product: the `argus` CLI, the LangGraph adapter, and AI-powered detection. ARGUS runs **fully local** — runs are stored in `.argus/runs/`, no account, no cloud, no signup. Heuristic detection (150+ signatures) works out of the box.

<details>
<summary>Installing only what you need</summary>

`argus-agents` has a zero-dependency core. Pull in just the extras you use:

```bash
pip install argus-agents            # framework-agnostic library only (ArgusSession)
pip install "argus-agents[cli]"     # + the `argus` command-line tool
pip install "argus-agents[langgraph]"  # + the ArgusWatcher LangGraph adapter
pip install "argus-agents[llm]"     # + LLM-powered semantic features (OpenAI/Anthropic/Google)
pip install "argus-agents[all]"     # everything (recommended)
```
</details>

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

## Getting Started (AI-Powered Setup)

The fastest way to set up ARGUS — let your AI assistant handle the integration:

**1. Go to [arguslabs.in](https://arguslabs.in)**

**2. Click on "AI Setup Prompt"**

<img src="https://github.com/VaradDurge/ARGUS/blob/master/assets/Argus%20Guidelines%20and%20Contribution.png?raw=true" width="700"/>

**3. Copy the prompt and paste it into your AI assistant** (Claude, ChatGPT, Cursor, etc.) — it will handle the full setup for you. This is the **most important step**.

**4. (Optional) Log in for hosted cloud sync**

```bash
argus login
```

Signing in with Google enables **hosted** cloud sync and the shared trends registry — part of the managed/enterprise tier. It is **entirely optional**: the open-source package is fully local and needs no login. (`argus login` reports "hosted-only feature" unless a hosted backend is configured.)

**5. Open the dashboard**

```bash
argus ui
```

That's it. ARGUS is set up and ready to go.

### After Setup

- Run your LangChain/LangGraph pipeline as usual (in terminal)
- Check the dashboard for your recent run (takes 1–2 seconds to appear — refresh if needed)

---

## Quick Start

```python
from argus import ArgusWatcher

watcher = ArgusWatcher(graph)       # attach to your StateGraph
app = graph.compile()
result = app.invoke(initial_state)
watcher.finalize()                  # persist the run to .argus/runs/
```

ARGUS monitors every node, detects failures, and saves the run. No changes to your node functions.

> **Always call `watcher.finalize()`** after `app.invoke()`. Required for cyclic graphs, safe for all. Without it the run stays in memory and won't appear in `argus list` or the dashboard.

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

Runs in order, each more expensive — only fires when needed:

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

---

## Semantic Judge

For subtle quality issues that pattern matching can't catch:

```python
watcher = ArgusWatcher(graph, semantic_judge=True)  # enabled by default
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
    semantic_judge=True,           # LLM judge on every node (default: True)
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
argus inspect <id> --step <node>     # dump raw input/output for a node
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

## Web Dashboard

```bash
argus ui    # opens at localhost:7842
```

Shows all runs, node-level detail, AI analysis, replay diffs, loop iteration badges, and comparison views. No account needed for local use.

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

**v0.8.11** — [changelog](https://github.com/VaradDurge/ARGUS/releases)

## License

ARGUS is **open-core**. The open-source core (`src/argus/`, the `argus-agents` PyPI
package) is licensed under **Apache-2.0** — see [LICENSE](LICENSE). The `cloud/`
directory (hosted/enterprise components) is proprietary — see [cloud/LICENSE](cloud/LICENSE).
