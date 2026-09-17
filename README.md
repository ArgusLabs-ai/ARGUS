<div align="center">
  <img src="https://github.com/VaradDurge/ARGUS/blob/master/assets/Argus-NameTrans.png?raw=true" width="480"/><br/>
  <a href="https://arguslabs.in"><img src="https://img.shields.io/badge/website-arguslabs.in-6366f1" alt="Website"/></a>
  <a href="https://pypi.org/project/argus-agents/"><img src="https://img.shields.io/pypi/v/argus-agents" alt="PyPI version"/></a>
  <a href="https://pypi.org/project/argus-agents/"><img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+"/></a>
  <a href="https://github.com/VaradDurge/ARGUS/releases"><img src="https://img.shields.io/badge/status-beta-6366f1" alt="Beta"/></a>
  <a href="https://discord.gg/67XTFTDSgd"><img src="https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white" alt="Discord"/></a>
</div>

---

**Catch silent failures in AI agent pipelines before production.**

Your LangGraph pipeline runs fine — no exception. But three nodes later, something crashes with a `KeyError`. The real cause? A node upstream silently dropped a field. ARGUS catches this.

Beta, and under active development. ARGUS is early. Expect rough edges and bugs, and expect things to move. Issues and pull requests are welcome. Contributors: join the [Discord](https://discord.gg/67XTFTDSgd) before opening a PR — that is where updates land.

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
from argus import ArgusRecorder
app = ArgusRecorder().attach(graph)
```

`attach()` returns the app you invoke. Nothing about your graph is patched or
rewritten — ARGUS rides LangGraph's own callback stream. See
[Which entry point?](#which-entry-point) if you are on the older `ArgusWatcher`.

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
from argus import ArgusRecorder

app = ArgusRecorder().attach(compiled_graph)   # returns the app you invoke
result = app.invoke(initial_state)             # run is persisted automatically
```

ARGUS records every node, grades the run, and saves it. No changes to your node
functions, and no changes to the graph either.

What it keeps is **the dict each node returned** — its update, before LangGraph
merges it into shared state. That is the whole trick. A node that searches,
throws the result away and returns `{}` leaves a full-looking merged state
behind; only the update shows the silent no-op, and only then can blame land on
the node that caused it instead of whoever crashes three steps later.

`invoke`, `ainvoke`, `stream`, `astream` and `batch` all work. One `attach()`
serves many runs — each `invoke`, and each `.batch()` item, gets its own run
file and its own verdict.

<a id="which-entry-point"></a>
### Which entry point?

| | `ArgusRecorder` **(use this)** | `ArgusWatcher` (legacy) |
|---|---|---|
| How it captures | Listens to LangGraph callbacks | Patches `compile()` and rebinds `invoke` / `stream` / `batch` |
| Touches your graph | No | Yes |
| Contract for "who needs this field" | Declared — `consumers=` | Read from successor type hints |
| Status | The path under active development | Still supported, not being extended |

`ArgusWatcher` keeps working and its docs below still apply to it. New code
should use `ArgusRecorder`.

### Declaring who reads what

A recording carries state, not code, so it cannot know that `write` three steps
later needs the `audience` field `plan` produced. Tell it:

```python
app = ArgusRecorder(consumers={"audience": ["write"]}).attach(graph)
```

Now a field that is **never written**, **written empty**, or **written and then
dropped** fails on the node responsible — not on the node that happened to
notice. Without a declaration ARGUS still catches empty updates, tool failures,
crashes and degraded output; `consumers=` is what adds long-range field
contracts.

It is also the answer for a **final** node: `empty_output` only fires when a
node has a successor waiting, so a last node that returns `{}` is exempt by
design (a terminal `send_email` legitimately returns nothing). Declare the field
the run is supposed to end with and that gap closes.

### `ArgusWatcher` (legacy path)

```python
from argus import ArgusWatcher

watcher = ArgusWatcher()
app = watcher.attach(graph)         # StateGraph or already-compiled app
result = app.invoke(initial_state)
```

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
| **Crash root cause** | Traces `KeyError` at node 5 back to the upstream node that actually dropped the field |
| **Wrong subject entirely** | Ingredients go in, a paragraph about helicopters comes out. Structurally perfect, semantically nonsense — the [judge](#semantic-judge) catches this |
| **Contract violations** | A field a later node needs was never written, written empty, or dropped in between — blamed on the node responsible ([`consumers=`](#declaring-who-reads-what)) |
| **Latency degradation** | Node takes 95%+ of timeout, or suspiciously fast LLM call (likely cached/empty) |
| **Conditional path confusion** | Unchosen branches correctly shown as "skipped" — not false "crashed" |

---

## Detection Layers

Runs in order, each more expensive — only fires when needed. Every status a layer can assign, and how node statuses roll up into the run verdict, is specified in [`docs/STATUS.md`](docs/STATUS.md).

1. **Heuristics** — 150+ failure signatures (placeholders, empty results, error keys, semantic degradation). Zero cost.
2. **Validators** — custom per-node business-logic constraints. Deterministic.
3. **Anomaly detector** — statistical checks for output size anomalies, timing outliers. Deterministic.
4. **Correlator** — traces failure propagation across nodes. Points at the *origin*, not the crash site.

   Blame is decided against the state each node really saw, read off the recording. Where ARGUS
   has to reconstruct that state — it replays your reducers from a saved *name*, since a reducer
   callable cannot be stored in a run file — and its reconstruction disagrees with what the run
   actually recorded, the recording wins. A custom reducer (anything that is not `operator.add`
   or `add_messages`) is therefore approximated, never trusted over the trace, so a node whose
   `[]` your reducer discards is not reported as having dropped anything.
5. **LLM semantic judge** — evidence-aware final ruling. Receives all signals from layers 1–4 before deciding. Cannot override validator failures or critical anomalies.
6. **LLM investigator** — root cause explanations and debugging suggestions. Only on ambiguous failures.

---

## Loop-Aware Inspection

Pipelines with loops (LLM -> compiler -> if fail, retry) get special treatment:

- Earlier iterations that self-corrected are marked `retried` (not counted as failures)
- Only the **final iteration** determines pass/fail
- Dashboard shows iteration badges, collapse/expand across attempts

---

## Replay

Fix a bug, re-run from the failing node. Skip upstream nodes entirely:

```bash
argus replay <run-id> node_7 --only --app mypkg.graph:build   # re-run that node against your graph
argus replay <run-id> node_7                                  # re-run from node_7 onward
argus diff <rerun-id>                                         # compare vs original
```

**A rerun takes the state from the run file and the code from you.** The input is the state
node_7 really saw, rebuilt from the steps that already passed — they are never re-executed. The
function comes from the compiled graph you pass with `--app` (a zero-arg callable returning it),
so the fix you just made is what runs. ARGUS never goes looking for your source to import it.

Runs recorded the older way (`ArgusWatcher`) stored references to their own node functions and
still replay without `--app`; those are labelled `(legacy refs)` in the header, and external API
calls made during them were recorded to cassettes, so their replays are free and deterministic.
On the trace path there are no cassettes — external calls execute live, and the command says so
before it runs.

To grade a saved run with no graph at all, that's `argus check <id>`, not replay.

### Time-Travel: edit the state, then resume

Spotted the bad value? Fix it in the saved state and resume from there — no code change, no re-running the steps that already worked:

```bash
argus replay <run-id> node_7 --set status=OK          # correct a value
argus replay <run-id> node_7 --delete stale_field     # reproduce a dropped field
argus replay <run-id> node_7 --patch fix.json         # a full patch document
argus replay <run-id> node_7 --set status=OK --dry-run  # preview, run nothing
```

The same rule applies: a trace run needs `--app` alongside these, `--dry-run` included — the
graph is required before the patch is previewed. Upstream nodes stay frozen, so only the resumed
trajectory changes. Paths are dotted with list
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

Pattern matching cannot tell you that a node fed cake ingredients wrote about
helicopter rotors. Nothing is missing, nothing is empty, no tool failed — the
output is simply about the wrong thing. That is what the judge is for.

```python
app = ArgusRecorder().attach(graph)                       # on when a key is set
app = ArgusRecorder(semantic_judge=False).attach(graph)   # rules only, fully deterministic
```

On by default once a provider key exists (`argus key set`, or `argus login`) —
setting a key is opt-in enough. With no key it stays off, since it could only
skip anyway.

### Judge last, never first

The judge runs **after** every deterministic layer and receives what they found.
It cannot overturn a validator failure or a critical anomaly, and — the rule
that matters most in CI — it mostly cannot **originate** a failure either:

| The judge says | Gates the build? |
|---|---|
| `unrelated` — output is about a different subject than the input | **Yes, on its own.** No rule can see this |
| `contradiction` — output contradicts the input or itself | **Yes, on its own** |
| `empty_or_missing`, or anything else | Only if a deterministic layer flagged that step too |

The reason is measured, not philosophical. Left free to fail anything it
disliked, the judge made the gate nondeterministic: one healthy
`create_react_agent` failed two runs in three, at confidence 1.0, with
self-contradicting reasons. Emptiness is a job the rules already do reliably, so
the judge only gets a vote there. Coherence is a job nothing else can do, so it
stands alone — and because it stands alone it has to prove itself **twice**, on
two independent samples, before failing a build.

An uncorroborated verdict is still recorded and shown by `argus show`; it just
does not move the status.

> Judging is skipped entirely for a turn that only issued tool calls — an empty
> `content` next to a populated `tool_calls` is how every tool-calling model
> works, and there is no prose there to rule on.

Every verdict carries an audit trail:

```json
{
  "pass": false,
  "reason": "The output is completely unrelated to the input, which is about ingredients for a recipe.",
  "failure_kind": "unrelated",
  "confidence": 1.0,
  "evidence_considered": ["validator:payment_check", "anomaly:BA-003"],
  "overridden_signals": []
}
```

- `failure_kind` — which of the four kinds above, deciding whether it can gate alone
- `evidence_considered` — which prior signals the judge weighed
- `overridden_signals` — which it disagreed with and passed despite

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

app = ArgusRecorder().attach(graph)   # ArgusConfig applies to ArgusWatcher today
watcher = ArgusWatcher(graph, config=config)
```

---

## CLI

```
argus list                           # all recorded runs
argus show last                      # most recent run
argus show <id>                      # inspect a specific run
argus check <id>                     # CI gate for an exact run; prints the JSON path checked
ARGUS_RUN_ID=<id> argus check        # CI-friendly selection when the id comes from an earlier step
argus check last                     # newest-file fallback — avoid in a shared workspace
argus check last --format json       # same verdict as one JSON object (run_id, overall_status, passed, findings[])
argus check last --fail-on crashed,silent_failure   # only these run statuses fail the gate
argus inspect <id> --step <node>     # dump raw input/output for a node
argus fix <id>                       # fix prompt for the root cause, ready to paste
argus replay <id> <node> --app m:fn  # re-run from a node, against the graph you pass
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

ARGUS auto-wraps `StateGraph.compile()` / compiled `invoke()` for the test session. A clean pipeline stays a passing test; missing fields, tool failures, crashes, and semantic degradation fail that test. Tests that never invoke a graph are unchanged. After a standalone CI run, pass its exact id with `argus check <id>` or `ARGUS_RUN_ID=<id> argus check`; `argus check last` only means the newest file and can select a stale or unrelated run in a shared workspace.

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

**v0.11.0** — [changelog](https://github.com/ArgusLabs-ai/ARGUS/releases)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Join the [Discord](https://discord.gg/67XTFTDSgd) before opening a PR for updates and to talk through the change.

## License

ARGUS is **open-core**. The open-source core (`src/argus/`, the `argus-agents` PyPI
package) is licensed under **Apache-2.0** — see [LICENSE](LICENSE). The `cloud/`
directory (hosted/enterprise components) is proprietary — see [cloud/LICENSE](cloud/LICENSE).
