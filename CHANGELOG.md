# Changelog

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
