# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## NOTE: ALWAYS REMEMBER
Never add a "co-authored by claude" while pushing or in CI/CD operation. It should always look like no AI assisted coding was used.

## Implementation pivot (active)

The **idea** is unchanged: catch silent failures, blame the origin node, fail CI before deploy (`argus check` / `pytest --argus`). Contributor brief: `docs/ARGUS-PIVOT.pdf`.

The **implementation** is pivoting. Do not extend Pregel wrapping as the default path.

- **Stop (as default):** patching `StateGraph.compile`, wrapping node functions in `patcher.py`, wrapping `invoke` / `stream` / `batch`, reading live successor `__annotations__` as the only contract, re-importing callables + urllib3 HTTP cassettes for replay.
- **Do instead:** ingest a **fat trace** (node name, input, **`output_update`** = the dict the node returned — not only merged LangGraph state — plus tools and errors). Grade with the existing inspector. Capture is a callback / OTel / SDK; the product is the verdict.
- **Layer 2 (new sub-branch):** traces do not include successor type hints. Adjacent “node N output vs node N+1 input” is not enough — node D may still need field `b` written by A. Build a **consumer map + ledger** (who wrote, who reads later, who dropped).
- **Layer 3:** ledger of running state **and** HTTP / tool I/O. Replay/record become ledger-based.
- **LLM judge last**, never first. Same role as today (`semantic_checker.py`). Cannot override validator failures or critical anomalies.
- **Unchanged:** tool-failure scan, signatures, `json_in_string`, anomalies, findings, `argus check`, `argus fix`, origin blame (if the ledger is complete).
- **Not this milestone:** GitHub App / auto-PRs, production Slack, cloud UI polish, new framework adapters. Those are later.

**Build order:** (1) fat recorder → existing inspector, no `patch_graph`; (2) ledger; (3) consumer-map contract; (4) judge → `argus check`. Spike 1 is done only when a demo graph fails `empty_output` / empty tools without `patcher.py`. Skinny traces (sampled, LLM-only, payloads stripped) are not enough — refuse to grade or require the fat recorder.

The Architecture section below describes **current** code (wrap path). New work follows this pivot unless a ticket explicitly says to patch the old path.

## Commands

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run single test
pytest tests/test_smoke.py::test_name -v

# Run tests with coverage
pytest --cov=src --cov-report=term-missing

# Lint
ruff check src/

# Format
ruff format src/

# Type check
mypy src/argus

# Run the CLI
argus --help
argus show <run-id>
argus check last
ARGUS_RUN_ID=<run-id> argus check
argus diff <run-a> <run-b>
argus replay <run-id> <node>
argus ui
pytest --argus                       # fail tests whose ARGUS run was not clean
# CI eat-own-cooking gate (#56):
# pytest tests/test_argus_ci_gate.py --argus
```

## Architecture

ARGUS is a production readiness platform for AI agent pipelines — detects silent failures, semantic degradation, and contract violations before deployment (LangGraph-first, framework-agnostic).

### Detection Pipeline

Every wrapped node executes through this pipeline:
1. Output captured and serialized
2. **Tool failure scan** (`inspector.py`): error keys, HTTP status codes, empty results, semantic registry — over the node's output dict *and* over the tool I/O the fat trace recorded for that step (`inspect_tool_calls`, fed into `on_node_end` by the recorder). A tool that raised is critical even when the node swallowed it and returned a plausible update; a tool payload is run through the same `inspect_tool_outputs` rules, so a 404 body or empty result set is caught. A plain-string tool result is left alone. Also `empty_output` (critical): a node returning a literal empty state update (`{}`) while successors wait downstream — the canonical silent no-op, blamed on the origin instead of the downstream crash site. Narrow by design: only literal `{}` is flagged; a dict with keys (even empty-valued, e.g. `{"vulnerabilities": []}`) is a real state contribution left to the per-field rules. Gated on `has_successors` (does any edge leave this node), **not** on `successor_fns` — a conditional source is handed no successor fns because its branches' annotations cannot all be required at once, but a worker that also owns the loop edge still has nodes waiting on it, and this rule never reads a successor's type hints. Also `json_in_string` (warning, Rule 17): a string field that parses as a JSON object/array — double-encoded JSON returned instead of a parsed structure. Advisory only (warning, not critical, so it never flips run status); fields where stringified payloads are expected (`raw_response`, `log`, `logs`, `raw`, `history`, `raw_output`, `payload`) are skipped case-insensitively.
3. **Structural inspection** (`inspector.py`): missing required fields vs successor type hints, type mismatches
4. **Semantic validators**: custom per-node or wildcard validators
5. **Anomaly detection**: behavioral anomaly signals (output size, timing, structure)
6. **LLM semantic judge** (`semantic_checker.py`): on by default when a key is configured. Returns a `failure_kind`; only `unrelated` / `contradiction` (semantic coherence — nothing else can detect it) may fail a step with no deterministic layer agreeing, and such a verdict is re-asked once and must reproduce. `empty_or_missing` / `other` need corroboration. Evidence-aware final ruling — receives all prior signals (validator results, anomaly signals, inspection findings) as context, plus the ledger rows *before* this step (`prior_rows`, #85) scoped to the fields the node reads or writes, so an upstream emptying is visible instead of only "this field is empty now". Cannot override validator failures or critical anomalies. Returns `evidence_considered` and `overridden_signals` for audit trail.
7. Status assigned: `pass | fail | crashed | semantic_fail | degraded_input | interrupted` (plus `retried` / `skipped` set at finalize). Full vocabulary and the node → run roll-up: `docs/STATUS.md`
8. `NodeEvent` recorded; auto-finalize if last node or error
9. At finalize, every per-step signal (inspection, validators, anomalies, judge, crash, tool-chain) is flattened into `RunRecord.findings` — one `Finding` per signal with a stable content-hash `id`, a full-sentence `reason`, and a `source`. Consumers read this list, not the step shapes (`findings.collect_findings`, schema_version "2"; older records are back-filled on load)

### Core Classes

- **`ArgusSession`** (`session.py`): Framework-agnostic core. Thread-safe, supports sync + async. Wrap nodes via `wrap()`, `instrument()`, or `@session.node()` decorator.
- **`ArgusWatcher`** (`watcher.py`): LangGraph adapter. Accepts a `StateGraph` as optional first arg: `ArgusWatcher(graph)` auto-attaches, or use `watch()`/`watch_compiled()` separately. All config params are keyword-only. Supports `record_http=True` for deterministic reruns.
- **`ArgusInspector`** (`inspector.py`): Static functions for `inspect_tool_outputs()`, `inspect_transition()`, and `build_root_cause_chain()`. Root cause analysis walks backward through events to find where a failing field was omitted.

### Key Files

| File | Role |
|------|------|
| `src/argus/recorder.py` | **Pivot path.** `ArgusRecorder` — fat-trace ingest via LangChain callbacks. `attach(app)` returns a `RunnableBinding` around the graph (**not** `with_config`: LangGraph's `ensure_config` overwrites the callbacks key instead of merging it, so bound callbacks are dropped the moment a caller passes its own — i.e. whenever the graph is composed into `prompt | app`, a tool, or a LangServe route, #87). The binding proxies `nodes` / `get_graph` / `stream` / `batch`, so callers still hold the graph. Nothing is patched. Keeps each node's *update*, not the merged state — unwrapping a `Command(goto=..., update=...)` handoff first (`_node_update`, #88), because it is not a dict and used to fall into the unreadable branch and lose the update entirely. `update=None` (routing and nothing else) stays `None`: the node claimed no update, which is not the same as claiming an empty one, and collapsing the two fails every working supervisor. A pair-sequence update is folded to a dict; anything that will not fold is returned untouched rather than raising, since a recorder that raises takes the user's graph down with it. Refuses to grade a thin trace (`IncompleteTraceError`). Topology via `_topology()` → `get_graph(xray=True)`, so nodes **inside a subgraph** are registered (bare names, matching what the callbacks report) instead of hiding behind one opaque parent node. The subgraph *parent* step is deliberately **not** recorded: LangGraph reports its "update" as the subgraph's whole merged state, which double-reports every inner finding and credits the parent with fields it never wrote. Because that row is absent, `_blame_barren_subgraphs` asks the one question no inner step can answer alone — did the subgraph write anything the **parent** graph can see? An inner-only key (present in the subgraph's schema, absent from the outer one) makes every inner update look non-empty while the parent state gains nothing and the next node reads it unchanged (`subgraph_no_contribution`, critical, #89). Subgraph-level on purpose: an early inner node writing a scratch key to feed a later one contributes nothing outward and is not a failure |
| `src/argus/grading.py` | **Pivot path.** The grading chain with no graph engine: `new_session()` (placeholder successors so `empty_output` still fires, judge flag, deferred finalize) and `finish()` (refuse an unfinished or empty trace with `IncompleteTraceError` → ledger → contextual blame → finalize). The recorder calls it; a trace-file ingest can too. Imports nothing from `langgraph` / `langchain_core`. Contextual reasons accumulate on the origin inspection without replacing an earlier structural or tool message |
| `src/argus/ingest/langsmith.py` | **Pivot path.** `argus ingest langsmith <file>` — grade a LangSmith JSONL export with no app; imports nothing from `langgraph` / `langchain_core`. Tool and model child runs become the step's `tool_calls` / `llm_usage`. `--edges` takes real topology from `argus edges`; without it successors are guessed from step order. `--consumers` wires the contextual layer. Refuses a skinny trace (`_refuse_skinny`: root run with no `outputs`, or a node run with no `inputs`) — except a root carrying an `error`, because a graph that raised has no final state to export. A step whose `outputs` is a serialised `Command` repr (`{"output": "Command(...)"}`) is recorded with **no** update plus a critical `unreadable_update` signal (`_command_repr`, #111): the repr omits `update` when falsy, so the silent no-op and legitimate routing are byte-identical, and grading it clean is the outcome the brief bans |
| `src/argus/ledger.py` | **Pivot path.** `build_ledger()` — steps folded into the notebook (input, update, running state, tools, error, status). Derived from `RunRecord.steps`, not a second store. Steps marked `skipped` (the unchosen branch of a conditional) are not rows — they never ran. Reduced fields accumulate via `RunRecord.reducer_kinds`, strings because reducer callables do not survive the run file. The running state is scoped to `RunRecord.state_keys` (the graph's own state keys): a subgraph node writes into the *subgraph's* schema, and a key living only there reaches no node outside it, so carrying it forward told a later reader a field was waiting that never was — and cleared the node `contextual` should have blamed. The row's `update` still reports what the node returned; only the notebook is scoped. Empty `state_keys` (an old run file, or trace-file ingest, which has no schema) folds everything as before |
| `src/argus/contextual.py` | **Pivot path.** Declared consumer map (`consumers={"field": ["reader"]}`) → blames the first step whose running state lacked a field a later node reads. Never written → the earliest step; written then dropped → the dropper. Never the reader, never the node merely adjacent to it. **Every** declared reader is checked (a field emptied between `price` and `ship` is caught at `ship`); a row that was itself starved of a declared field is a **victim** and is not blamed for what it then wrote empty (`retrieve` → `rerank` → `generate`: one finding, on `retrieve`); a reader that writes the field it reads is exempt only if it had something to read or wrote a value — a filter turning an absent field into `[]` is starved, not a producer. `{"field": {"readers": [...], "allow_empty": True}}` declares presence-only (`issues: []` is the LGTM path) |
| `src/argus/session.py` | Core monitoring session, wraps arbitrary callables |
| `src/argus/watcher.py` | LangGraph adapter (thin wrapper over `ArgusSession`) — legacy wrap path |
| `src/argus/pytest_instrument.py` | pytest `--argus` auto-wrap of `StateGraph.compile()` / all Pregel runtime methods (`invoke` / `ainvoke` / `stream` / `astream` / `batch` / `abatch`) |
| `src/argus/inspector.py` | Silent failure detection + root cause chain |
| `src/argus/registry.py` | Semantic signature registry for LLM output heuristics |
| `src/argus/models.py` | Dataclasses: `NodeEvent`, `RunRecord`, `InspectionResult`, `LLMUsage` |
| `src/argus/storage.py` | Persist/load `RunRecord` to `.argus/runs/<run-id>.json` |
| `src/argus/patcher.py` | Patch LangGraph node functions; handle LG 0.2+ and legacy formats |
| `src/argus/llm_tracker.py` | Extract token usage from node output metadata |
| `cloud/pricing.py` | Cost calculation per model (enterprise) |
| `src/argus/http_recorder.py` | HTTP recording/playback for deterministic reruns |
| `src/argus/replay.py` | Rerun engine with reducer-aware state merging. Every path takes the node's input from the **ledger row**, never from `record.steps` directly. `replay_live(run_id, node, app=...)` is the pivot-path rerun: input from the notebook, function off the caller's compiled graph (`app.nodes[n].bound`, read not wrapped). No app → refuses and points at `argus check`; a trace holds state, not code |
| `src/argus/llm_proxy.py` | Shared LLM transport — all chat completion calls go through here. Resolves BYOK (OpenAI/Anthropic/Google) first, falls back to hosted Supabase proxy |
| `src/argus/providers.py` | Per-provider request/response translation for BYOK (message format, model remapping, response normalization) |
| `src/argus/signature_generalizer.py` | Generalizes failure signatures via LLM + heuristic fallback. Uses `llm_proxy` for the LLM path |
| `src/argus/check.py` | CI gate: evaluate a `RunRecord` as clean vs crash / silent_failure / semantic_fail |
| `src/argus/cli/cmd_check.py` | `argus check <id>` / `ARGUS_RUN_ID=<id> argus check` / `argus check last` — grade one run, print its file, and exit 1 when it was not clean |
| `src/argus/pytest_plugin.py` | pytest `--argus` plugin: fail tests whose instrumented run was not clean |
| `tests/test_argus_ci_gate.py` | Narrow sync-invoke graph run under CI `pytest --argus` (eat-own-cooking; #56) |
| `src/argus/cli/main.py` | `argus` CLI entry point (Typer) |
| `src/argus/cli/cmd_doctor.py` | `argus doctor` diagnostic command |
| `src/argus/findings.py` | `collect_findings()` — builds `RunRecord.findings`; also the one-line terminal summary after invoke |
| `src/argus/data/signatures.json` | Bundled semantic failure signatures |

### Semantic Signature Registry (`registry.py` + `data/signatures.json`)

Detects placeholder/degraded LLM outputs using match strategies:
- `exact_ci`, `contains_ci`, `prefix_ci`: string matching
- `regex`: compiled pattern
- `repetition`: n-gram repetition detection
- `semantic_similarity`: embedding cosine match (6 bundled signatures use it). **Opt-in — set `ARGUS_EMBEDDINGS=1`.** Off, these signatures are inactive and `heuristic_coverage()` says so; on, every scanned string value is POSTed to the OpenAI embeddings API (a node's real output data leaving the process) and cached in `.argus/embeddings_cache.db`. `embedding_store` never reads a `.env` file — the key comes from `argus key set` or an env var already in the process.

Categories: `placeholder_outputs`, `null_like_semantic`, `suspicious_phrases`, `corrupted_markers`, `repeated_filler`, `malformed_payload`, `empty_semantic_state`, `semantic_refusal` → mapped to `placeholder_detected`, `semantic_degradation`, or `structural_anomaly` failure types by `_CATEGORY_TO_FAILURE` in `inspector.py`.

### LLM Transport

All LLM chat completion calls route through `llm_proxy.create_chat_completion`. It tries BYOK first (user's own OpenAI/Anthropic/Google key via `argus key set`), then falls back to the hosted Supabase proxy for logged-in users. `providers.py` handles per-provider translation (message format, model names, response shapes). No call site should use its own client directly — `signature_generalizer` was the last holdout and was moved onto this path.

### Storage

Runs are stored in `.argus/runs/<run-id>.json` relative to the working directory. Cloud sync to Supabase (non-blocking background thread) if the user is logged in via `argus login`.

An attached `ArgusWatcher` reuses one `ArgusSession` across calls (its node wrappers close over it). Each outermost `invoke()` / `stream()` / `batch()` on an already-completed session calls `ArgusSession.begin_new_run()` to persist its own `RunRecord` (and re-arm the HTTP recorder) — so every invoke gets a run, not just the first. Nested calls (batch items, stream internals) defer finalize and stay part of the outer run.

### Root Cause Analysis

`build_root_cause_chain(steps_so_far)` in `inspector.py`:
- Phase 1 (crash): Traces `KeyError` crash back to node that omitted the missing field. Extracted as `crash_origins(steps, edge_map)` so blame can act on it, not just report it — `ArgusSession._blame_crash_origins` marks that node's inspection so `argus check` names it. A `KeyError` states its own field, so this needs no declared consumer map. The correlator's `root_cause_chain` override is skipped on crashed runs: it diffs input→output and so can only ever nominate the crash site
- Phase 1b (nested crash): `state["policy"]["number"]` raises `KeyError 'number'` and `number` is nobody's state field, so the top-level walk finds nothing and used to blame whichever node ran last. `_nested_container_origin` instead names the node that wrote the container the crashed node read — the empty dict first (`{"policy": {}}`, the cache-miss shape), then most-recently-written. Nothing fits → no blame, rather than a bystander
- Phase 2 (silent): Walks backward through `InspectionResult.missing_fields`
- Handles parallel fan-out (doesn't blame a field if any sibling provided it)
- Returns deduplicated ordered list of culprit node names

### Website / UI

`website/` contains a Next.js dashboard served by `argus ui`. Key components:
- `app/compare/DiffView.tsx`: Side-by-side run diff view
- `components/CliRunView.tsx`: Single-run detail view

### Testing

Tests live in `tests/test_smoke.py`. Marks used: `@pytest.mark.unit`, `@pytest.mark.integration`. Run all or target single tests by function name.

`tests/test_silent_failure_matrix.py` is the pivot path's detection + false-positive matrix: 39 tests over eight real LangGraph pipelines (supervisor loop, map-reduce fan-out, CRM triage, tool fetcher, degraded output, crash handoff, subgraph, subgraph-with-its-own-schema), each asserting *which node* is blamed. `patch_graph` is monkeypatched to raise throughout. Run it before and after any change to `inspector.py` / `contextual.py` / `recorder.py` — roughly half its tests assert a pipeline is **clean**, so it catches a rule that started over-firing as readily as one that stopped firing.

`tests/test_shipped_shapes_matrix.py` is its companion (29 tests), covering the shapes that matrix deliberately left out: `create_react_agent` (prebuilt, tool-call round trip), `MessagesState` / `add_messages`, and `.batch()` / repeat `invoke` on one recorder, plus the judge-corroboration rule. Scripted fake model, same contract (nothing patched, every test names the blamed node). Each of those three shapes was hiding a defect — see the table in `docs/PIVOT-BRANCH.md`. Run **both** matrices after any detection change: this one is the guard on message-envelope false positives (`response_metadata`, stringified tool results), on pass-through user text being read as a model refusal, on one `attach` serving many runs, and on the judge-corroboration rule (a judge `fail` only moves a step's status when a deterministic layer flagged that step too — see `docs/PIVOT-BRANCH.md`).
