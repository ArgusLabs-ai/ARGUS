# PRD: ARGUS — production readiness platform for AI agent pipelines

| | |
|---|---|
| **Status** | Draft v1 — 2026-08-30 |
| **Author** | Abhishek Sharma (co-maintainer) |
| **Reviewers** | @VaradDurge (owner) |
| **Baseline** | `argus-agents` 0.10.5, `dev` branch, 29★ / 8 forks / 6 open issues |
| **Evidence** | [EVIDENCE.md](EVIDENCE.md) — every requirement tagged `[E-n]` traces to a note there |
| **Dependencies** | [DEPENDENCY-MAP.md](DEPENDENCY-MAP.md) |

---

## 1. Introduction

ARGUS wraps every node of an AI agent pipeline (LangGraph-first, framework-agnostic via
`ArgusSession`), captures input/output state, and flags failures that *don't throw*: dropped
fields, placeholder text, refusals, empty `{}` updates, contract violations, and degraded
upstream data that surfaces as a crash three nodes later. It ships a CLI (`argus show/check/
replay/diff/fix`), a pytest plugin (`pytest --argus`), a local web dashboard (`argus ui`), and an
optional BYOK LLM judge.

**The problem this PRD solves is not a missing feature — it's a missing spine.** The project has
a strong detection engine and a growing contributor base, but:

- The status vocabulary and run-JSON shape are undocumented, so every downstream consumer
  (CI harness, dashboard, exporter) hand-parses internals `[E-1]`.
- Real users hit false positives with no suppression mechanism, and real failures (6 of 10 in
  the fault matrix) pass silently `[E-4]`.
- The dashboard advertises five pages that don't exist `[E-9]`.
- Contribution paths exist (fixtures, signatures) but fixtures aren't wired into tests, so a
  contributor can't see their fixture *do* anything `[E-5]`.
- Roadmap decisions live in chat logs, `good-to-have-dev/`, and a 42 KB UI spec — nothing ties
  them together for a new contributor or an AI coding agent `[E-7]`.

This document is that spine. It is written so a junior developer or an AI agent can pick any
user story and implement it without asking what "done" means.

## 2. Goals

Measurable, in priority order:

- **G1 — Stable contract.** Publish the status vocabulary and a versioned run-JSON schema with
  a top-level `findings[]` list. Every CLI verdict available as `--format json`.
- **G2 — Trustworthy detection.** Raise heuristic-only recall on the fault-injection matrix from
  **4/10 → 8/10** and add a suppression mechanism so false positives cost one command, not a
  content rewrite.
- **G3 — Five-minute activation.** A new user sees ARGUS catch a real silent failure within
  5 minutes of `pip install`, without wiring their own graph.
- **G4 — Honest dashboard.** Zero "soon" surfaces; every visible page backed by shipped data.
- **G5 — Contributor ladder.** A first-time contributor can land a fixture PR in one sitting and
  see it exercised in CI. Median first-PR-to-merge ≤ 7 days.
- **G6 — Exportable.** Findings leave ARGUS via webhook and OpenTelemetry without custom code.

## 3. Personas

| Persona | Who | What they need from ARGUS |
|---|---|---|
| **P1 Pipeline dev** *(primary)* `[E-5]` | Python developer shipping a multi-node LangGraph pipeline; may use AI coding assistants heavily | Catch silent failures locally, gate CI, get a paste-ready fix prompt |
| **P2 Platform/QA engineer** | Runs many agents repeatedly (the 50-agent test bed) | Tags, JSON verdicts, cross-run diffs, hotspot view, suppression config |
| **P3 First-time contributor** | Found ARGUS on GitHub; has a broken agent output to share | One-command fixture contribution, visible CI proof, clear "good first issue"s |
| **P4 Maintainer** | @VaradDurge, @abhisheksharma001 | Fewer stale docs, owner-gated paths don't block releases, PRs that update docs with code |
| **P5 AI coding agent** | Claude Code / Cursor operating on this repo | Unambiguous acceptance criteria, file-level pointers, doc-coupling rules |

## 4. User stories

Each story is sized for one PR. Acceptance criteria are verifiable. `[E-n]` links evidence.
Owner-gated stories are marked **[owner]**.

### 4.1 Detection core (W1)

#### US-1.1: Document the status vocabulary `[E-1]`
**Description:** As a P2 engineer, I want a published list of every step and run status and how
step statuses roll up, so I can write stable assertions.
**Acceptance:**
- [ ] `docs/STATUS.md` lists every step status (`pass | fail | crashed | semantic_fail | degraded_input | interrupted | retried | skipped`) and every run status (`clean | crash | silent_failure | semantic_fail`) with one-line definitions
- [ ] A roll-up table: which step status → which run status; explicit rule for `retried` (healthy iff final iteration passes) and for warnings (see US-1.4)
- [ ] `models.py` status literals are `typing.Literal` types matching the doc exactly
- [ ] `README.md` "Detection Layers" links to `docs/STATUS.md`
- [ ] Lint + tests pass

#### US-1.2: Normalized `findings[]` on `RunRecord` `[E-1]`
**Description:** As any consumer, I want one flat list of findings per run so I don't walk
`steps[].semantic_check / anomaly_signals / validator_results` and know each shape.
**Acceptance:**
- [ ] `RunRecord.findings: list[Finding]` where `Finding = {id, node, type, severity, reason, field_path?, origin_node?, confidence?, source: "heuristic"|"validator"|"anomaly"|"llm"}`
- [ ] Populated in `ArgusSession.finalize()` from existing per-step data; no detection logic changes
- [ ] `reason` is a full sentence readable without seeing the node ("Field `documents` was dropped by `search`; `retrieve` expected it")
- [ ] `storage.py` bumps `schema_version`; loading older files back-fills `findings` on read
- [ ] `website/lib/types.ts` mirrors `Finding`
- [ ] `CLAUDE.md` key-files table + Detection Pipeline step 8 updated
- [ ] Tests: one fixture run round-trips with ≥3 findings of different `source`

#### US-1.3: Signature suppression config `[E-4]`
**Description:** As P1/P2, when a signature flags legitimate content (e.g. `NL-002` on the string
`"none"`), I want to silence it per node or per project instead of rewriting my output.
**Acceptance:**
- [ ] `argus ignore <SIG-ID> [--node <name>]` writes to `.argus/config.json` under `suppressions`
- [ ] `argus ignore --list` and `argus ignore --remove <SIG-ID> [--node]`
- [ ] `registry.py` consults suppressions before emitting; suppressed hits are recorded as `suppressed: true` in the finding (not dropped) so `argus stats` can still count them
- [ ] `argus doctor` prints active suppressions
- [ ] README CLI block + `argus --help` updated
- [ ] Test: suppressed signature does not change run status; unsuppressed does

#### US-1.4: Warning escalation policy `[E-1][E-4]`
**Description:** As P1, I want to decide when warning-severity signals should fail a run, because
today degraded text stays `clean`.
**Acceptance:**
- [ ] `ArgusConfig.strict: Literal["off","warn_as_fail","critical_only"]`, default `critical_only` (current behavior)
- [ ] `argus check --strict warn_as_fail` overrides config for CI
- [ ] `has_tool_warnings: bool` added next to `has_tool_failure`
- [ ] `docs/STATUS.md` roll-up table updated
- [ ] Test: same run yields `clean` under default and `silent_failure` under `warn_as_fail`

#### US-1.5: Make latency checks reachable by default
**Description:** As P1, I expect `suspiciously_fast` to be able to fire without discovering an
undocumented kwarg.
**Acceptance:**
- [ ] `ArgusConfig.min_expected_ms` default `200`; `ArgusWatcher(min_expected_ms=…)` accepted
- [ ] Only applies to nodes ARGUS identifies as LLM-calling (has `LLMUsage`) — no false positives on pure-Python nodes
- [ ] README Configuration block updated
- [ ] Test: a 5 ms LLM node fires `suspiciously_fast`; a 5 ms non-LLM node does not

#### US-1.6: Substring tier for placeholder signatures `[E-4]`
**Description:** As P1, I want `"TBD"` embedded in a paragraph to be caught, not only when a
field is exactly `"TBD"`.
**Acceptance:**
- [ ] New match strategy `contains_token_ci` — matches a signature only at word boundaries (`\bTBD\b`), case-insensitive
- [ ] Applied to PH-* placeholder signatures as a **warning** (exact match stays critical)
- [ ] Fixture added under `fixtures/placeholder_in_prose/` that old code misses, new code catches
- [ ] `tests/test_false_positives.py` extended: `"Standard TBD-compliant format"` style strings do NOT match

#### US-1.7: Detect swallowed exceptions (constant fallback after error-shaped flow)
**Description:** As P1, if my node catches its own exception and returns a constant fallback,
I want a warning, because today it looks like a pass.
**Acceptance:**
- [ ] Heuristic: node output identical (deep-equal) across ≥2 invocations in the same run **and** `LLMUsage` shows a request was made → `constant_fallback` warning
- [ ] Documented in `docs/STATUS.md` and `CLAUDE.md` Detection Pipeline
- [ ] Fixture + test

#### US-1.8: Consumer-aware empty-update detection (partial)
**Description:** As P1, when a node returns `{"vulnerabilities": []}` and its successor's type
hints require non-empty, I want a warning.
**Acceptance:**
- [ ] `inspector.py`: if successor annotation is a non-Optional collection and value is empty → `empty_required_collection` warning
- [ ] Does not fire when successor has no annotations (stays conservative)
- [ ] Fixture + test

#### US-1.9: Per-signature precision from dispute data `[E-4]`
**Description:** As P4, I want `argus stats` to show, per signature, hits vs. disputes so I can
retire noisy ones.
**Acceptance:**
- [ ] `argus stats` table gains `hits`, `disputed`, `precision = 1 - disputed/hits`
- [ ] Signatures with precision < 0.5 and ≥10 hits are marked `⚠ noisy`
- [ ] `argus stats --json`
- [ ] Test using seeded `feedback_store`

### 4.2 CLI contract (W2)

#### US-2.1: `argus check --format json` `[E-1][E-3]`
**Acceptance:**
- [ ] Emits `{run_id, status, findings: [...], schema_version}` (findings from US-1.2)
- [ ] Exit code unchanged (1 on non-clean); JSON goes to stdout, human text suppressed
- [ ] `--fail-on silent_failure,semantic_fail` filter (comma list); default = all non-clean
- [ ] `tests/test_cmd_check.py` covers JSON shape and `--fail-on`

#### US-2.2: Run tags and filters `[E-3]`
**Acceptance:**
- [ ] `ArgusWatcher(tags={"agent": "04_sentiment", "suite": "live"})` and `ArgusSession(tags=…)`
- [ ] `ARGUS_TAGS="k=v,k2=v2"` env var merges in
- [ ] `RunRecord.tags: dict[str,str]` persisted
- [ ] `argus list --tag agent=04_sentiment`; `argus show last --tag suite=live` resolves last *matching* run
- [ ] Documented in README CLI + Configuration

#### US-2.3: `argus show last` deterministic under concurrency
**Acceptance:**
- [ ] "last" resolves by `finalized_at` timestamp inside the record, tie-broken by run id — not file mtime
- [ ] Test: two records written within 1 ms resolve consistently

#### US-2.4: `argus diff` regression mode
**Acceptance:**
- [ ] `argus diff <a> <b> --regression` prints: status delta, findings added/removed (by `Finding.id`), latency delta per node > 20 %
- [ ] Exit 1 if any finding was added or status worsened
- [ ] `--format json`
- [ ] Test on two fixture runs

#### US-2.5: `argus fixture add <run-id> [--node <name>]` `[E-3][E-5]`
**Description:** As P1/P3, I want to turn a caught failure into a committed regression fixture
with one command.
**Acceptance:**
- [ ] Writes `fixtures/<failure_type>/runs/<run-id>.json` in the format `fixtures/README.md` defines, stripping secrets (keys matching `/key|token|secret|password/i`)
- [ ] Prompts for a one-line `notes` if not `--notes`
- [ ] Prints the `git add` + PR hint from CONTRIBUTING
- [ ] `fixtures/README.md` updated to mention the command

#### US-2.6: `argus demo` `[E-6]`
**Description:** As a new user, I want to see a real catch in under a minute.
**Acceptance:**
- [ ] Bundled 4-node LangGraph pipeline in `src/argus/data/demo/` where node 2 silently drops `documents`; no network, no key needed
- [ ] `argus demo` runs it, prints the standard `[argus] run … silent_failure on retrieve` finding, then `argus show last | argus ui` hint
- [ ] `--open` launches `argus ui` on the demo run
- [ ] README step 4 mentions `argus demo` as the first thing to try
- [ ] Test asserts the demo run is non-clean and names the right origin node

#### US-2.7: Determinism hint for scripted/cached LLMs
**Acceptance:**
- [ ] `argus check --expect-nondeterministic`: if two runs with the same graph + input have byte-identical LLM node outputs, print a warning (not a failure)
- [ ] Documented as opt-in

### 4.3 Contributor experience (W3)

#### US-3.1: Fixtures exercised by tests `[E-5]`
**Acceptance:**
- [ ] `tests/test_fixtures.py` parametrizes over every `fixtures/**/runs/*.json`
- [ ] Each fixture dir has `fixture_spec.md` with `expected: {status, failure_type, origin_node}`; test asserts against it
- [ ] Fixture with missing spec fails the test with a message pointing to `fixtures/README.md`
- [ ] `CONTRIBUTING.md` "Adding Fixture Runs" updated: "your fixture will run in CI on your PR"

#### US-3.2: Contributor ladder in CONTRIBUTING `[E-5]`
**Acceptance:**
- [ ] Section "Your first four PRs": (1) fixture, (2) signature in `signatures.json` + fixture, (3) detection rule in `inspector.py` + fixture + test, (4) module owner (added to CODEOWNERS for a path)
- [ ] Each rung links to one open `good first issue`
- [ ] Time estimate per rung

#### US-3.3: Issue templates for fixture and signature contributions **[owner]**
**Acceptance:**
- [ ] `.github/ISSUE_TEMPLATE/fixture.yml` — fields: what the agent did, output dict, what ARGUS reported, what you expected
- [ ] `.github/ISSUE_TEMPLATE/signature.yml` — fields: phrase/pattern, provider, example output
- [ ] Bug template placeholder version bumped to current

#### US-3.4: Unify author identity; CODEOWNERS by area **[owner]**
**Acceptance:**
- [ ] `.mailmap` maps both Abhishek identities to one
- [ ] CODEOWNERS adds path-level owners: `/src/argus/replay.py /src/argus/state_patch.py /src/argus/llm_proxy.py /src/argus/providers.py @abhisheksharma001`
- [ ] Rationale comment kept in file

#### US-3.5: Stale-reference sweep (docs)
**Acceptance:**
- [ ] README badges/links → `ArgusLabs-ai/ARGUS`; footer version reads from a single place or is removed
- [ ] CONTRIBUTING signature count matches `signatures.json` (72) or is replaced by "see `argus stats`"
- [ ] `good-to-have-dev/README.md` "Already shipped" table no longer lists `loop_analyzer.py`; live-loop-guard row says "not built"
- [ ] A CI step (US-6.2) greps for `VaradDurge/ARGUS` in `README.md` and fails

### 4.4 Dashboard (W4) `[E-9][E-10]`

#### US-4.1: Remove or gate `soon` navigation
**Acceptance:**
- [ ] `Sidebar.tsx`: Traces, Evaluation, Graphs, Alerts, Datasets removed from nav
- [ ] `EvaluationBuilder.tsx` "(soon)" panel removed from run detail
- [ ] `?preview=1` query param restores them for maintainers (documented in `website/README.md`)
- [ ] CONTRIBUTING "Web UI — Planned Pages" reworded: pages are planned, not stubbed
- [ ] Verify in browser using dev-browser skill

#### US-4.2: Findings panel on run detail `[E-1]`
**Acceptance:**
- [ ] New `components/run-detail/FindingsPanel.tsx` renders `RunRecord.findings` **grouped by severity** with a coloured left rule and header count ("Critical (2) / Warning (4)"), node order within a group `[E-10a]`
- [ ] Suppressed findings in a greyed "Suppressed (n)" group at the bottom
- [ ] Right-aligned per-row action "Fix prompt" → `argus fix` output for that node
- [ ] Each row: severity chip (existing `StatusBadge` colours), node, `reason` full sentence, `origin_node` link, `source` tag, suppressed rows greyed
- [ ] Click → scrolls to that step in `StepInspector`
- [ ] `RootCauseBanner` reads from `findings[0]` when present
- [ ] Verify in browser using dev-browser skill

#### US-4.3: Tag filter on run list `[E-3]`
**Acceptance:**
- [ ] `RunTable.tsx` gains a filter-chip row above the table: `tag: value ×` per active filter + "+ Add filter" opening a per-key multi-select `[E-10c]`; state in URL params
- [ ] Empty state text when no runs match filter
- [ ] Verify in browser using dev-browser skill

#### US-4.4: Failure hotspot matrix `[E-2]`
**Description:** As P2, I want to see, across all runs, which (origin node → failing node)
pairs fail most often.
**Acceptance:**
- [ ] API `GET /api/hotspots?tag=…` in `cmd_open_ui.py` aggregates `findings[].origin_node × node` counts across runs
- [ ] `components/HotspotMatrix.tsx`: rows = origin, cols = failing node, cell = raw count on a **single-hue amber ramp**, zero cells blank, "Low → High" legend, one-line explainer under title `[E-10b]`; click → run list filtered to those runs
- [ ] Lives on the Runs page above the table, collapsed by default when < 5 runs
- [ ] No LLM call involved
- [ ] Verify in browser using dev-browser skill

#### US-4.5: Empty state points to `argus demo` `[E-6]`
**Acceptance:**
- [ ] `EmptyRunsState.tsx`: primary = copyable `argus demo --open` command block; secondary = "Attach to my graph" → Guide; existing "reading from `<path>`" line kept under both; no illustration `[E-10d]`
- [ ] Verify in browser using dev-browser skill

#### US-4.6: Design tokens audit
**Acceptance:**
- [ ] `globals.css` defines semantic colour tokens for the five statuses in README (crashed red, silent amber, semantic purple, degraded orange, skipped grey); no raw Tailwind palette classes in `components/run-detail/*`
- [ ] Dark/light both verified
- [ ] Verify in browser using dev-browser skill

### 4.5 Integrations (W5) `[E-3]`

#### US-5.1: Webhook exporter
**Acceptance:**
- [ ] `ArgusConfig.webhook_url` / `ARGUS_WEBHOOK_URL`; POST on finalize with the US-2.1 JSON body; non-blocking background thread like cloud sync
- [ ] `only_on: ["silent_failure","crash"]` filter
- [ ] Retries 3× with backoff; failures logged, never raise into user code
- [ ] `src/argus/exporters/webhook.py`; `CLAUDE.md` key-files table
- [ ] Test with a local HTTP server fixture

#### US-5.2: OpenTelemetry exporter **[owner: pyproject]**
**Acceptance:**
- [ ] Optional extra `argus-agents[otel]` → `opentelemetry-sdk`
- [ ] One span per run, child span per node; findings as span events; status → span status
- [ ] `ArgusConfig.otel_endpoint`; if extra not installed and endpoint set → clear error from `argus doctor`
- [ ] Docs page `docs/integrations/opentelemetry.md`

#### US-5.3: GitHub Actions example
**Acceptance:**
- [ ] `docs/ci/github-actions.md` + `examples/ci/argus-check.yml`: run pipeline, `argus check last --format json --strict warn_as_fail`, upload run JSON as artifact
- [ ] README CLI section links to it

### 4.6 CI, release, governance (W6) `[E-8]`

#### US-6.1: Fix environment-dependent `TestCLI`
**Acceptance:**
- [ ] `tests/test_e2e_langgraph.py::TestCLI` uses `sys.executable` (or `argus` entry point) instead of system `python3`
- [ ] Passes in a fresh venv on macOS and in CI

#### US-6.2: CI hardening **[owner]**
**Acceptance:**
- [ ] `mypy src/argus` job (currently listed in `CLAUDE.md` but not run)
- [ ] Coverage floor: `pytest --cov=src --cov-fail-under=<current-5>`; raise 5 pts per release
- [ ] Stale-link grep from US-3.5
- [ ] Matrix unchanged (3.9 / 3.11 / 3.12)

#### US-6.3: Release cadence documented
**Acceptance:**
- [ ] `CONTRIBUTING.md` "Releases": patch releases on merge of any `bugfix`; minor every 2 weeks if `enhancement` merged; owner performs; co-maintainer may request via issue labelled `release-request`
- [ ] `CHANGELOG.md` entries required in the PR that lands the change (not at release time)

#### US-6.4: Publish run JSON schema `[E-1]`
**Acceptance:**
- [ ] `docs/schema/run.schema.json` (JSON Schema draft 2020-12) generated from `models.py` dataclasses via a script in `scripts/`
- [ ] CI fails if regenerated schema differs from committed
- [ ] README "Web Dashboard" links to the schema

## 5. Functional requirements (numbered, cross-referenced)

- **FR-1** The system must expose a closed, documented status vocabulary (US-1.1).
- **FR-2** Every finalized run must carry a flat `findings[]` list whose entries have a stable `id`, `type`, `severity`, `reason` sentence, and `source` (US-1.2).
- **FR-3** Users must be able to suppress a signature per node or per project via CLI, persisted in `.argus/config.json`; suppressed hits remain counted (US-1.3).
- **FR-4** Warning-severity findings must be escalatable to failures through a config/CLI strictness setting (US-1.4).
- **FR-5** Latency anomaly checks must have a default that can fire on LLM nodes without extra configuration (US-1.5).
- **FR-6** Placeholder signatures must match at word boundaries inside prose as warnings (US-1.6).
- **FR-7** `argus check` must emit machine-readable JSON with the same exit-code semantics as the human form (US-2.1).
- **FR-8** Runs must accept string tags at construction or via env, persisted and filterable in CLI and UI (US-2.2, US-4.3).
- **FR-9** `argus show last` must resolve deterministically under concurrent writers (US-2.3).
- **FR-10** `argus diff --regression` must return non-zero when findings are added or status worsens (US-2.4).
- **FR-11** A caught failure must be convertible into a committed fixture with one command that strips secrets (US-2.5).
- **FR-12** `argus demo` must produce a non-clean run offline in under 60 s (US-2.6).
- **FR-13** Every fixture under `fixtures/` must be executed by the test suite against a declared expectation (US-3.1).
- **FR-14** The dashboard must not display navigation to pages that have no backing feature (US-4.1).
- **FR-15** The dashboard must render `findings[]` as the primary failure view on run detail (US-4.2).
- **FR-16** The dashboard must offer a cross-run origin×failing-node hotspot matrix without LLM calls (US-4.4).
- **FR-17** Findings must be exportable via webhook (POST) and OpenTelemetry spans, non-blocking to user code (US-5.1, US-5.2).
- **FR-18** CI must run lint, type-check, tests, coverage floor, and stale-link checks (US-6.2).
- **FR-19** A JSON Schema for run records must be published and kept in sync by CI (US-6.4).
- **FR-20** Any PR that changes a public surface must update the coupled docs listed in `DEPENDENCY-MAP.md` §3.

## 6. Non-goals (out of scope for this PRD)

- **N-1** No changes to `cloud/` or `supabase/`. Hosted proxy, pricing, login flows are owner-only and proprietary.
- **N-2** No new framework adapters (CrewAI, ADK, AutoGen, LlamaIndex, Haystack, DSPy, SmolAgents). API must stabilize first (`CONTRIBUTING.md`). `ArgusSession.wrap()` remains the manual path.
- **N-3** No live loop guard / mid-run intervention. Post-hoc only (see `good-to-have-dev/live-loop-guard.md`).
- **N-4** No 1–5 quality scores or "quality over time" charts on the dashboard until per-signature precision (US-1.9) exists `[E-1][E-4]`.
- **N-5** No Traces, Evaluation, Graphs, Alerts, Datasets pages in this cycle. They come back one at a time, each with its own PRD section, once US-4.1 lands.
- **N-6** No Datadog/New Relic/Prometheus/Slack/PagerDuty exporters yet — webhook covers them via user-side glue; OTel covers the collector path.
- **N-7** No LLM-required detection paths added. Every new rule in this PRD works heuristic-only; LLM judge stays opt-in.
- **N-8** No rewrite of `TRIAL_UI_SPEC.md` demo site; it's a marketing artifact, separate from `argus ui`.

## 7. Design considerations

- **Vocabulary first.** Reuse existing words (`silent_failure`, `degraded_input`, `origin`). Do not introduce "issue", "alert", "error" as synonyms.
- **Terminal finding format is the design system's root.** `findings.py` output — `[argus] run 8f3a1c02  silent_failure on retrieve / missing: documents (dropped by search)` — is the pattern every other surface (JSON, dashboard row, webhook body) mirrors: *status on node / what / caused by whom / next command*.
- **Colours** are already specified in README (crashed red, silent amber, semantic purple, degraded orange, skipped grey). Tokenize them (US-4.6); don't invent new ones.
- **Empty and error states** must always say where ARGUS is reading from (`$ARGUS_DIR` / project root) — existing behaviour, keep it.
- **Visual references** for US-4.2 / 4.3 / 4.4 / 4.5 are in `EVIDENCE.md` E-10 (Mobbin, 2026-08-30). Summary: findings grouped by severity with header counts (Semrush); single-hue amber hotspot grid with blank zero cells (Klaviyo/Maze); removable `tag: value ×` filter chips above the run table (Aboard/Twenty); empty state = copyable `argus demo` block + "attach to my graph" (Steep/Grok). Any *new* dashboard surface needs its own pass first.

## 8. Technical considerations

- **Schema versioning.** `storage.py` already has `schema_version`. US-1.2 bumps it; loaders must accept N-1. Never break reading of old `.argus/runs/*.json`.
- **Thread safety.** `ArgusSession` is thread-safe; exporters must follow the cloud-sync pattern (background thread, never raise).
- **Python 3.9 floor.** No `match`, no `X | Y` in runtime annotations without `from __future__ import annotations`.
- **Zero new required deps.** OTel is an extra. Webhook uses stdlib `urllib` like `llm_proxy`.
- **Owner-gated files.** `pyproject.toml`, `.github/`. Batch: US-3.3 + US-3.4 + US-6.2 + the OTel extra can go as one owner PR.
- **LangGraph ≥ 0.2** wrapper semantics (`patcher.py`) unchanged by anything here.
- **Performance.** Hotspot aggregation reads every run file; cap at last 500 runs and cache by mtime in `cmd_open_ui.py`.

## 9. Success metrics

| Metric | Baseline (2026-08-30) | Target | How measured |
|---|---|---|---|
| Fault-matrix recall, heuristic-only `[E-4]` | 4 / 10 | 8 / 10 | `faults/_run_matrix.py` on each release |
| False-positive noise in clean runs | BA-001/BA-005 fire on fully clean runs | 0 critical, ≤1 warning per clean run of the 50-agent bed | `~/argus-agents-lab check_suite.py` |
| Time to first caught failure for a new user `[E-6]` | unmeasured (requires own graph) | ≤ 5 min via `argus demo` | manual timing, 3 testers |
| `soon` surfaces in dashboard `[E-9]` | 5 nav + 1 panel | 0 | grep `soon` in `website/` |
| Fixture PRs exercised in CI | 0 % | 100 % | `tests/test_fixtures.py` |
| Median first-PR-to-merge (external) | unmeasured | ≤ 7 days | `gh pr list --json createdAt,mergedAt` |
| Downstream consumers hand-parsing run JSON | all | 0 — they use `--format json` or the schema | issue tracker |
| Stale doc references | 6 known (DEPENDENCY-MAP §5) | 0, CI-enforced | US-6.2 grep |
| Test suite env-independence | 2 failures on non-venv `python3` | 0 | CI + fresh venv |

## 10. Sequencing (suggested; each step = one PR, per "How We Work")

1. US-6.1 (unblock local test runs) → US-1.1 → US-1.2 → US-2.1 *(critical path)*
2. US-1.3 → US-1.4 → US-1.5 → US-1.6 *(recall + noise)*
3. US-3.1 → US-3.5 → US-3.2 *(contributor loop)*
4. US-2.6 → US-4.5 → US-4.1 *(activation + honesty)*
5. US-2.2 → US-2.3 → US-4.3 → US-4.2 *(tags + findings UI)*
6. US-5.1 → US-5.3 → US-6.4 *(export + schema)*
7. Owner batch: US-3.3, US-3.4, US-6.2, OTel extra for US-5.2
8. US-1.7, US-1.8, US-1.9, US-2.4, US-2.5, US-2.7, US-4.4, US-4.6, US-6.3 *(fill-ins, any order)*

## 11. Open questions

- **Q1** Should `findings[].id` be stable across ARGUS versions (content hash of `type+node+field_path`) or per-run UUID? Regression diff (US-2.4) needs the former; leaning content hash.
- **Q2** `argus demo` bundles a LangGraph graph — is `langgraph` guaranteed present (it's a hard dep today) or should the demo use `ArgusSession` only so it works if the dep is ever made optional?
- **Q3** Owner's appetite for path-level CODEOWNERS (US-3.4) vs. keeping `*` shared — governance call.
- **Q4** OTel semantic conventions for GenAI are still evolving; pin to `gen_ai.*` attributes or ARGUS-namespaced? Decide before US-5.2.
- **Q5** Should suppression config (US-1.3) live in `.argus/config.json` (per project, committable) or `~/.argus/` (per user)? Proposal: project, so a team shares it.
- **Q6** ~~Mobbin pass outstanding for all W4 stories.~~ Done — E-10. Compare view (`app/compare/`) not covered; do a pass if it enters scope.
