# Changelog

## 0.16.0 — 2026-10-21

- `pytest --argus-report-only` (or `ARGUS_REPORT_ONLY=1`) records and grades every run but never fails a test on its verdict. The runs a session saved are listed in `.argus/session_runs.txt`
- `argus cloud upload [--final]` sends a pytest session's run cards to a team workspace (`ARGUS_CLOUD_URL`). It proves which repository is calling with the GitHub Actions OIDC token, so there is no secret to set up (`permissions: id-token: write`). Any error fails the job with one line; nothing is retried silently. A run sent to a workspace is not also sent as telemetry
- GitHub Action: `cloud: "true"` with `cloud-url:` runs your tests report-only, then uploads. The job fails only on a test's own failure or a failed upload
- The run card (`argus.cloud_card.build_card`, schema `data/cloud_card.schema.json`) is what a workspace receives: verdict, findings without their reason, and the shape of the data, never a value. See `docs/DATA.md`
- `argus fix --sanitized` no longer leaks nested keys, top-level keys, input-state keys, path segments, keys that contain a dot, or text a learned signature quoted
- Runs record `agent` and `environment`: `ArgusRecorder(agent=)`, `$ARGUS_AGENT` and `$ARGUS_ENVIRONMENT` (`ci` on GitHub Actions)

## 0.15.0 — 2026-10-16

- Judge verdicts record the temperature, seed and `system_fingerprint` they were made with, plus whether they are `deterministic` (temperature 0, and a seed the provider honored). Set `ARGUS_JUDGE_SEED` or `ArgusConfig(judge_seed=)`. `argus show` / `check` / `diff` report it. Audit only, never gates
- With a seed set, the run reviewer and the semantic judge cache their verdicts in `.argus/verdict_cache.db`, so a re-run with the same input skips the LLM call. Rows are HMAC-signed with a per-install key. Turn it off with `ARGUS_VERDICT_CACHE=0`
- GitHub Action: the job summary names the root-cause node and carries a sanitized `argus fix` prompt for each failed run. The failed runs are uploaded as an `argus-runs-*` artifact (`upload-runs: "false"` to skip). A gate that graded nothing now says so
- `argus replay` exits 1 on every error
- `argus fix` no longer describes `return {}` as a failed external call or tells you to retry it

## 0.14.1 — 2026-10-14

- Runs record whether they finished: `complete` and `end_reason` (`finished`, `exception`, `interrupted`, `cancelled`, `unknown`). A graph that raised outside every node, an abandoned stream, Ctrl-C or a run saved at exit is `complete: false`. Recorded only, never gates

## 0.14.0 — 2026-10-14

- Tool failures carry a retry class: `fatal`, `transient` or `rate_limited`. `argus fix` advises to match
- A bare 429 inside an ID or count no longer reads as a rate limit
- Validators can return a score; `argus stats` lists scores, `argus diff` compares them. Low scores warn, never fail

## 0.13.0 — 2026-10-14

Anonymous telemetry, and a sturdier semantic judge.

- After each run Argus sends one anonymous shape card: types, lengths and hashed names, never values. Turn it off with `argus telemetry off`, `ARGUS_TELEMETRY=0` or `DO_NOT_TRACK=1`. See `docs/DATA.md`
- `argus telemetry status | on | off | show-last`. Every card is saved to `.argus/outbox/` before it is sent
- The semantic judge returns its verdict through a forced tool call (OpenAI, Anthropic, Gemini) instead of free text. A cut-off verdict no longer counts
- Fixed: two finalizes at once could save a run without its judge verdicts

## 0.12.2 — 2026-10-13

Privacy fix: pipeline data no longer leaves your machine unless you opt in.

- `argus login` no longer uploads every run. Full runs sync only with `ARGUS_CLOUD_SYNC=1`
- Shared signatures carry the pattern and counts only: no evidence, reasoning, node names or email
- The hosted LLM fallback (which sends real values in the prompt) runs only with `ARGUS_HOSTED_LLM=1`. Your own key (BYOK) works as before
- `ArgusRecorder(redact_keys=...)` keeps chosen fields out of the run file

## 0.12.1 — 2026-10-06

- README: the quickstart says how to turn on the run reviewer (a key plus node purposes); image links point at ArgusLabs-ai/ARGUS; the validators example uses `ArgusRecorder`. No code changes

## 0.12.0 — 2026-10-05

- `ArgusRecorder().attach(graph)` — fat-trace capture through LangChain callbacks; nothing in LangGraph is patched. Keeps each node's update, its tool calls and model calls. `ArgusWatcher` still ships as the legacy wrap path
- Whole-trace rules D1–D17: typo'd state keys, vendor error bodies, empty lookups, unfollowed pagination, healthy-baseline regressions (`argus baseline`), unrendered templates, repetition, truncated output used, unparseable model JSON, ungrounded numbers, near-miss IDs, claimed actions with no tool call, stuck loops, overstated status
- Consumer map (`consumers={"field": ["reader"]}`, dotted paths allowed): a field never written, written empty or dropped before a reader is blamed on the node responsible, never on the reader
- Run reviewer (#149): with node purposes (`purposes=` or `argus baseline --purposes`), an LLM checker and verifier must agree before a heuristic finding fails CI; a finding no rule saw fails only when two models verify it. Any LLM error falls back to the rules
- `pytest --argus` records through LangChain's configure hook with nothing patched (#78), and binds each run to the test that produced it (#152)
- `argus ingest langsmith` grades a LangSmith export with no app; `argus edges`, `argus consumers`; `report_tool_call()` for tools that bypass callbacks
- Dashboard redesign (#71, #155, #156), wired to fat traces: each step's tool calls and run-reviewer verdicts, a label for every finding type, rerun from the failing step through your app factory (#157)
- GitHub Action: `uses: ArgusLabs-ai/ARGUS/.github/actions/argus-gate@v0.12.0` (#57)
- `argus ignore` (#62), `argus check --strict warn_as_fail` (#77), `argus doctor` checks run storage writability, keys and disk usage (#154)
- Fixed: dataclass / pydantic state recorded as `{}` inputs; a crashing rerun is kept as a crashed run instead of an error; an `--app` factory returning `graph.compile()` is accepted

## 0.11.0 — 2026-09-07

- `RunRecord.findings` — one Finding per inspection, validator, anomaly, judge, crash, and tool-chain signal (stable id, full-sentence reason, source). Older records are back-filled on load (#60)
- `argus check --format json` and `--fail-on crashed,silent_failure,interrupted` (#61)
- `argus check` grades the requested run instead of the newest one (#64)
- `argus replay` accepts `--set`, `--delete`, `--patch`, and `--dry-run` to patch recorded state before rerunning
- `pytest --argus` watches `ainvoke` / `stream` / `astream` (#66)
- CI runs `pytest tests/test_argus_ci_gate.py --argus` on every PR (#68)
- Behavior profiles `chat_response` and `code_generation` (#65)
- Truncation, stale-context, and trailing `etc.` signatures (#43, #45)
- Warning-severity semantic signals no longer fail a node on their own (#47)
- Dashboard `/runs/<id>` deep links (#63)

## 0.10.5 — 2026-08-25

- Rule 17 — double-encoded JSON detection: a string field that parses as a JSON object/array is flagged `json_in_string` (warning). Advisory only; fields where stringified payloads are expected (`raw_response`, `log`, `payload`, …) are skipped (#26)

## 0.10.4 — 2026-08-25

- `argus show <id>` now resolves the requested run instead of always rendering the newest one; unknown ids and ambiguous prefixes exit 1 with a clean message instead of a traceback (#33, #39)

## 0.10.3 — 2026-08-25

- `argus key set` / `argus key use` now exit 1 when the operation fails (unknown provider, no saved key), so scripts gating BYOK setup on exit codes see failures (#40)

## 0.10.2 — 2026-08-25

- Wave E: retry/cycle false positives, validator severity, replay verdict, confidence explanation (#32)
- `empty_output` failure: a node returning a literal `{}` with successors downstream is flagged critical and blamed on the origin, not the downstream crash site (#31)
- Per-invoke persistence: an attached `ArgusWatcher` now writes a separate run record for every `invoke()` / `stream()` / `batch()`, not just the first
- Restore the `test_no_ordering_anomaly_correct_order` case that had been shadowed as dead code; single-source the correlator confidence weights and `_INACTIVE_STATUSES` constant

## 0.9.4 — 2026-08-25

- `empty_output` failure: a node returning a literal `{}` with successors downstream is flagged critical and blamed on the origin, not the downstream crash site — fixes silent no-op nodes (#31). Only literal `{}` is flagged; dicts with keys (even empty-valued) and router/conditional nodes are exempt
- Per-invoke persistence: an attached `ArgusWatcher` now writes a separate run record for every `invoke()` / `stream()` / `batch()`, not just the first (via `ArgusSession.begin_new_run()`)
## 0.10.1 — 2026-08-22

Version alignment. PyPI treated `0.10.0` (14 Aug, same era as `0.9.1`) as latest because `0.10.0 > 0.9.3`, so `pip install argus-agents` skipped the `0.9.2` / `0.9.3` work. This release republishes that work as `0.10.1`.

- `argus check last` / `argus check <id>` — CI gate; exit 1 on crash, silent failure, semantic fail, missing fields, or tool failures
- `pytest --argus` — auto-wrap LangGraph `compile()` / `invoke()` in tests; unclean runs fail the test
- `argus init` writes Cursor/Claude skills that attach `ArgusWatcher` and debug from `.argus/runs`
- Failed runs print a short `[argus]` finding; clean runs stay silent
- Empty dashboard shows which `.argus` path is served

## 0.9.3 — 2026-08-18

- `argus check last` / `argus check <id>` — CI gate; exit 1 on crash, silent failure, semantic fail, missing fields, or tool failures
- `pytest --argus` — auto-wrap LangGraph `compile()` / `invoke()` in tests; unclean runs fail the test
- README, guide, and argus-debug skill document the CI gate and pytest plugin

## 0.9.2 — 2026-08-16

- `argus init` writes Cursor/Claude skills that attach `ArgusWatcher` and debug from `.argus/runs`
- Failed runs print a short `[argus]` finding; clean runs stay silent
- Empty dashboard shows which `.argus` path is served

## 0.9.1 — 2026-08-14

- `pip install argus-agents` ships CLI, LangGraph adapter, and UI
- `ArgusWatcher.attach(graph)` is the one API (StateGraph or compiled); `invoke()` / `batch()` / `stream()` persist — no `finalize()` required
- LLM judge is opt-in via `argus key set` (or env); no `[llm]` extra. No key → heuristics only
- README, guide, and arguslabs.in docs match install + attach

## 0.4.4 — 2026-05-26

- Detection engine expanded: 7 new failure patterns across truncated outputs, retrieval quality, hallucinated success, confidence-behavior mismatch, shallow summaries, partial tool failures, corrupted structured output
- Single-node replay: re-run one node in isolation without rebuilding the full graph
- Inline before/after diff shown directly in CLI after replay
- AI investigation wired into replayed runs (uses stored node refs, no factory needed)
- Semantic signature registry extended: 14 new entries covering hedging phrases, placeholder variants, corrupted markers
- Short-string repetition detection fixed (N/A × 4 and similar patterns now caught)
- Replay tree visible in run detail Overview tab
- README and logo updated

## 0.4.3 — 2026-05-22

- Factory-free replay stabilised: node function refs stored per-run, no `--app` flag required for new runs
- Master-detail UI: resizable split panel, runs list on left, full run detail on right
- Overview tab: status card, pipeline overview, metrics grid, AI analysis summary, execution timeline, replay branches
- Pipeline tab: node-by-node step inspector with expandable input/output
- AI Analysis tab: LLM root cause investigation panel
- Correlations, State, Logs tabs added
- `argus replay` inline diff output

## 0.4.0 (beta) — 2026-05-17

- LLM token/cost tracking: auto-extracts usage from node outputs, shows cost per node and total in the UI
- Redesigned web dashboard: new sidebar, topbar, run detail layout, compare view
- Replay from subdirectories: runs recorded from child folders are now found and replayable from project root
- "Report the Dev" page in the UI for bug reports and feature requests
- Changelog page in the UI with full version timeline
- Guide page with setup and usage walkthrough
- Restored and expanded test suite (10 smoke tests)
- Fixed all ruff lint errors

## 0.3.10

- Replay system wired into web UI: click "replay from here" on any node
- Factory-free replay: auto-imports node functions from stored refs, no `--app` flag needed
- Polling-based replay status in UI with progress indicators

## 0.3.9

- CLI-only auth via `argus login` (removed Google login from web UI)

## 0.3.8

- Evaluation builder UI (placeholder)
- Rebuilt dashboard

## 0.3.7

- Auto-login in UI from CLI credentials
- Stitch interrupted runs in `argus show`

## 0.3.5

- Google login and Supabase cloud storage
- Background sync of runs to cloud

## 0.3.4

- Dashboard enhancements
- `argus login` for cloud features

## 0.3.3

- Web dashboard via `argus ui` (static Next.js, no Node required at runtime)

## 0.3.2

- Strict mode for inspector
- Nested error scan in tool outputs
- Generic list type checking

## 0.3.1

- Replay uses frozen node outputs instead of live LLM calls
- Improved error messages and label consistency

## 0.3.0

- Parallel and async node execution support

## 0.2.2

- Run differentiator: `argus diff <run-a> <run-b>`

## 0.2.1

- Tool call silent failure detection

## 0.1.0 (MVP)

- Core monitoring: `ArgusWatcher` for LangGraph, `ArgusSession` for any framework
- Silent failure detection: missing fields, type mismatches, empty outputs
- Semantic signature registry for placeholder/degraded LLM outputs
- Root cause analysis chain
- CLI: `argus show`, `argus replay`
- Local storage in `.argus/runs/`
