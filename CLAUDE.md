# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## NOTE: ALWAYS REMEMBER
Never add a "co-authored by claude" while pushing or in CI/CD operation. It should always look like no AI assisted coding was used.

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
argus diff <run-a> <run-b>
argus replay <run-id> <node>
argus ui
pytest --argus                       # fail tests whose ARGUS run was not clean
```

## Architecture

ARGUS is a production readiness platform for AI agent pipelines — detects silent failures, semantic degradation, and contract violations before deployment (LangGraph-first, framework-agnostic).

### Detection Pipeline

Every wrapped node executes through this pipeline:
1. Output captured and serialized
2. **Tool failure scan** (`inspector.py`): error keys, HTTP status codes, empty results, semantic registry. Also `empty_output` (critical): a node returning a literal empty state update (`{}`) while successors wait downstream — the canonical silent no-op, blamed on the origin instead of the downstream crash site. Narrow by design: only literal `{}` is flagged; a dict with keys (even empty-valued, e.g. `{"vulnerabilities": []}`) is a real state contribution left to the per-field rules, and router/conditional nodes (`successor_fns=[]`) are exempt. Also `json_in_string` (warning, Rule 17): a string field that parses as a JSON object/array — double-encoded JSON returned instead of a parsed structure. Advisory only (warning, not critical, so it never flips run status); fields where stringified payloads are expected (`raw_response`, `log`, `logs`, `raw`, `history`, `raw_output`, `payload`) are skipped case-insensitively.
3. **Structural inspection** (`inspector.py`): missing required fields vs successor type hints, type mismatches
4. **Semantic validators**: custom per-node or wildcard validators
5. **Anomaly detection**: behavioral anomaly signals (output size, timing, structure)
6. **LLM semantic judge** (`semantic_checker.py`): evidence-aware final ruling — receives all prior signals (validator results, anomaly signals, inspection findings) as context. Cannot override validator failures or critical anomalies. Returns `evidence_considered` and `overridden_signals` for audit trail.
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
| `src/argus/session.py` | Core monitoring session, wraps arbitrary callables |
| `src/argus/watcher.py` | LangGraph adapter (thin wrapper over `ArgusSession`) |
| `src/argus/pytest_instrument.py` | pytest `--argus` auto-wrap of `StateGraph.compile()` / compiled `invoke()` |
| `src/argus/inspector.py` | Silent failure detection + root cause chain |
| `src/argus/registry.py` | Semantic signature registry for LLM output heuristics |
| `src/argus/models.py` | Dataclasses: `NodeEvent`, `RunRecord`, `InspectionResult`, `LLMUsage` |
| `src/argus/storage.py` | Persist/load `RunRecord` to `.argus/runs/<run-id>.json` |
| `src/argus/patcher.py` | Patch LangGraph node functions; handle LG 0.2+ and legacy formats |
| `src/argus/llm_tracker.py` | Extract token usage from node output metadata |
| `cloud/pricing.py` | Cost calculation per model (enterprise) |
| `src/argus/http_recorder.py` | HTTP recording/playback for deterministic reruns |
| `src/argus/replay.py` | Rerun engine with reducer-aware state merging |
| `src/argus/llm_proxy.py` | Shared LLM transport — all chat completion calls go through here. Resolves BYOK (OpenAI/Anthropic/Google) first, falls back to hosted Supabase proxy |
| `src/argus/providers.py` | Per-provider request/response translation for BYOK (message format, model remapping, response normalization) |
| `src/argus/signature_generalizer.py` | Generalizes failure signatures via LLM + heuristic fallback. Uses `llm_proxy` for the LLM path |
| `src/argus/check.py` | CI gate: evaluate a `RunRecord` as clean vs crash / silent_failure / semantic_fail |
| `src/argus/cli/cmd_check.py` | `argus check last` / `argus check <id>` — exit 1 when the run was not clean |
| `src/argus/pytest_plugin.py` | pytest `--argus` plugin: fail tests whose instrumented invoke was not clean |
| `src/argus/cli/main.py` | `argus` CLI entry point (Typer) |
| `src/argus/cli/cmd_doctor.py` | `argus doctor` diagnostic command |
| `src/argus/findings.py` | `collect_findings()` — builds `RunRecord.findings`; also the one-line terminal summary after invoke |
| `src/argus/data/signatures.json` | Bundled semantic failure signatures |

### Semantic Signature Registry (`registry.py` + `data/signatures.json`)

Detects placeholder/degraded LLM outputs using match strategies:
- `exact_ci`, `contains_ci`, `prefix_ci`: string matching
- `regex`: compiled pattern
- `repetition`: n-gram repetition detection

Categories: `placeholder_outputs`, `null_like_semantic`, `suspicious_phrases`, `corrupted_markers`, `repeated_filler_text`, `malformed_embedded_json` → mapped to `placeholder_detected` or `semantic_degradation` failure types.

### LLM Transport

All LLM chat completion calls route through `llm_proxy.create_chat_completion`. It tries BYOK first (user's own OpenAI/Anthropic/Google key via `argus key set`), then falls back to the hosted Supabase proxy for logged-in users. `providers.py` handles per-provider translation (message format, model names, response shapes). No call site should use its own client directly — `signature_generalizer` was the last holdout and was moved onto this path.

### Storage

Runs are stored in `.argus/runs/<run-id>.json` relative to the working directory. Cloud sync to Supabase (non-blocking background thread) if the user is logged in via `argus login`.

An attached `ArgusWatcher` reuses one `ArgusSession` across calls (its node wrappers close over it). Each outermost `invoke()` / `stream()` / `batch()` on an already-completed session calls `ArgusSession.begin_new_run()` to persist its own `RunRecord` (and re-arm the HTTP recorder) — so every invoke gets a run, not just the first. Nested calls (batch items, stream internals) defer finalize and stay part of the outer run.

### Root Cause Analysis

`build_root_cause_chain(steps_so_far)` in `inspector.py`:
- Phase 1 (crash): Traces `KeyError` crash back to node that omitted the missing field
- Phase 2 (silent): Walks backward through `InspectionResult.missing_fields`
- Handles parallel fan-out (doesn't blame a field if any sibling provided it)
- Returns deduplicated ordered list of culprit node names

### Website / UI

`website/` contains a Next.js dashboard served by `argus ui`. Key components:
- `app/compare/DiffView.tsx`: Side-by-side run diff view
- `components/CliRunView.tsx`: Single-run detail view

### Testing

Tests live in `tests/test_smoke.py`. Marks used: `@pytest.mark.unit`, `@pytest.mark.integration`. Run all or target single tests by function name.
