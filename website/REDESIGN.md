# Dashboard Redesign — Work Plan

Branch: `dashboard-redesign` (off `master`)
Spec: **"Argus Instrument"** artifact — https://claude.ai/code/artifact/890ebef5-354e-4049-b4ad-cc6d989d1fbc
Full HTML saved for reference during build.

## Framing

This is a **reskin, not a rebuild.** The dashboard (`website/`, Next.js 14 + Tailwind)
already uses a CSS-variable token system and already has every component the spec
needs. The artifact defines an Apple/macOS visual language (SF Pro + SF Mono, system
blue accent, dual light/dark theme, signal colors per failure type). We map our
existing components onto that language — mostly CSS token + class changes, not new UI.

### Component mapping (spec section → existing file)

| Spec section | Existing component |
|---|---|
| Rail (sidebar) | `components/Sidebar.tsx` |
| Chips / status | `components/StatusBadge.tsx`, `EvalBadge.tsx` |
| Stat tiles + meter | `components/run-detail/MetricsGrid.tsx`, `RunMetricsBar.tsx` |
| Table (`tbl`) | `components/RunTable.tsx`, `RunListPanel.tsx` |
| JSON view | `components/JsonViewer.tsx` |
| Diff | `app/compare/components/StructuredDiff.tsx` |
| Log | `components/CliLogViewer.tsx` |
| Timeline | `components/run-detail/ExecutionTimeline.tsx` |
| Banner / root cause | `components/run-detail/RootCauseBanner.tsx` |
| Graph canvas | `components/run-detail/ExecutionGraph.tsx` (`@xyflow/react`) |
| Buttons / inputs / tabs / segmented | `components/ui/`, `Topbar.tsx`, tab navs |
| Empty / modal / toast | `EmptyRunsState.tsx`, `SendReportDialog.tsx` |

## Phases

**Phase 0 — Tokens (foundation, do first, unblocks everything)**
- Port the spec's full token set into `app/globals.css`: grounds (`--rail/--panel/--void/--line`),
  ink hierarchy (`--ink..--ink-4`), accent (`--iris`), signals (`--ok/--tool/--quality/--semantic/--coherence`),
  surfaces, shadows, radii, easings — for `:root` (light), `@media(prefers-color-scheme:dark)`, and `[data-theme]`.
- Bridge old → new names in `tailwind.config.ts` so existing `bg-primary`/`text-muted` classes keep working
  (alias `--primary`→`--iris`, `--muted-foreground`→`--ink-3`, etc.). Avoids a big class churn.
- Fonts: swap Geist/JetBrains → SF Pro/SF Mono stacks (`--sans`, `--mono`). System fonts = no bundle add.
- **Checkpoint:** app builds, both themes render, nothing visually broken (just re-tinted).

**Phase 1 — Primitives** (`ui/`, buttons, inputs, switch, tabs, chip, kbd, tooltip)
- Restyle to spec: 32px controls, `--r-ctl` radii, `.btn` variants (primary/ghost/outline/danger), focus rings.
- `StatusBadge` → spec `.chip-*` variants incl. `chip-run` breathing dot.

**Phase 2 — Data display** (tiles, table, json, diff, log, timeline, kv)
- Apply `.tile`/`.meter`, `.tbl` selectable rows, `.json` syntax colors, `.diff` add/del gutters, `.log` grid, `.tline`.

**Phase 3 — Feedback + Graph**
- `RootCauseBanner` → `.rc` culprit-chain styling; banners `.b-*`; empty/modal/toast.
- `ExecutionGraph`: restyle xyflow nodes to spec `.gnode` states (`s-crashed/s-fail/...`), dotted canvas, zoom bar.
  Keep xyflow — do NOT hand-roll pan/zoom.

**Phase 4 — Layout + polish**
- Shell grid (236px rail), masthead, section rhythm, scroll-reveal (respect `prefers-reduced-motion`).
- Theme toggle wired to `[data-theme]`.

## Ground rules
- Token-first: every color reads a var, no literals in components (spec's own rule).
- Reuse before rebuild — the components exist; restyle them.
- One phase = one reviewable commit. Verify build + both themes each phase.
- Don't add deps. xyflow, radix, lucide, tailwind already cover it.

## Phase 2b — Coverage gaps from recent PRs (fold into the phases above)

Audited `master` (PRs #34–#67) against the dashboard. TypeScript compiles clean
(`tsc --noEmit` → 0 errors) and all 8 step statuses render, but the UI has drifted
behind the backend on six points. G1–G3 are data-correctness bugs visible today —
worth fixing regardless of the redesign.

**G1 · `RunRecord.findings` is never rendered** (PR #60) — *highest impact*
`findings` is the canonical normalized failure list; CLAUDE.md states "Consumers read
this list, not the step shapes." It's typed at `lib/types.ts:328` with **zero render
sites**. The dashboard still reconstructs failures by walking step shapes, so the UI's
failure view can silently disagree with `argus check` / `argus show`.
→ Make findings the primary failure feed. Maps to spec `.rc` + `.banner` components.

**G2 · Two headline failure types render as a grey "Unknown" chip**
`empty_output` (PR #36) and `json_in_string` (Rule 17, PR #34) are missing from both
the `ToolFailure.failure_type` union (`lib/types.ts`) and `FAILURE_META`
(`lib/failure-labels.ts`) → `getFailureMeta()` hits `FALLBACK` = `'Unknown'`.
They appear in `/changelog` prose but are unlabeled in the actual run view.
→ Add both. `empty_output` = Tool/critical, `json_in_string` = Quality/warning (advisory).

**G3 · Two new behavior profiles unlabeled** (PR #65)
`chat_response` and `code_generation` missing from the `BehaviorType` union and
`BEHAVIOR_LABELS` (`BehaviorPanel.tsx:8`) → render as raw snake_case.

**G4 · `argus check` has no UI surface** (PRs #61, #64)
`check` is the CI gate (`--format json`, `--fail-on`) but no run page answers
"would this run fail CI?". Derivable client-side once G1 lands.
→ Add a verdict chip to the run header (spec `.chip-ok` / `.chip-tool`).

**G5 · `argus stats` absent from the dashboard entirely**
Signature effectiveness reporting incl. `--dispute` / `--prune` / `--disable` — a whole
feature surface with no UI and no mention in `/guide`. (`argus locate` is fine —
its `node_fn_paths` output renders in `StepRow.tsx:219`.)
→ Out of scope for the reskin; log as follow-up unless you want it in.

**G6 · Typed-but-absent `RunRecord` fields**
`state_patch` (PR #29 time-travel), `coverage_summary`, `schema_version`,
`app_factory_ref` are on the Python model but not the TS interface. Low priority.

Sequencing: G2/G3 are ~10-line data fixes → do in **Phase 0** alongside tokens.
G1 + G4 are real UI work → **Phase 3** (feedback/root-cause). G5/G6 → backlog.

## Open questions (confirm before Phase 1)
- [ ] Is the artifact the final visual target, or a starting direction to iterate on?
- [ ] Keep the current information architecture (pages/tabs) as-is, or does layout change too?
- [ ] Theme toggle: add a user switch, or follow OS only?
