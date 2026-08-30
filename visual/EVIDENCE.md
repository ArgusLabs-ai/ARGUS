# Evidence — why the PRD says what it says

Format follows the `visual-and-research` habit: look at what strong teams do *before* building.
Two source classes were used:

1. **Operator interviews & essays** — Lenny's Newsletter / Lenny's Podcast archive (MCP search,
   2026-08-30). Cited as *Title (who, date) url*.
2. **Field data from this repo** — `ARGUS_STABILITY_REVIEW.md` (50 LangGraph agents, 150 nodes),
   fault-injection recall matrix, open issues, and the codebase itself.

> Visual references for the four dashboard surfaces in scope are in **E-10** (Mobbin pass done
> 2026-08-30). Any *new* dashboard surface still needs its own pass before build.

---

## E-1 — Detection core: binary verdicts, critique as payload, one judge per failure

**Pattern to use:** Every ARGUS signal resolves to a binary pass/fail with a human-readable
critique attached. Numeric confidence stays as *supporting* data, never the headline.

**What operators say:**
- "The domain expert reviews each user interaction and writes a free-form critique … as well as
  giving a pass/fail judgment … the critique should be detailed enough for a brand-new employee
  at your company to understand it … Being too terse is a common mistake."
  — *Building eval systems that improve your AI product* (Hamel Husain & Shreya Shankar, 2025-09-09)
  https://www.lennysnewsletter.com/p/building-eval-systems-that-improve-your-ai-product
- "Aim for a manageable set of under 10 primary failure modes … The goal is to create a useful
  taxonomy that you can analyze, not an exhaustive list." — same source.
- "One judge per specific failure, binary." — *Why AI evals are the hottest new skill* (same
  authors, podcast 2025-09-25) https://www.youtube.com/watch?v=BsWxPI9UM4c

**Changes to the plan:**
- PRD FR-1.x: publish a **closed status vocabulary** (≤10 failure types). The stability review
  found the vocabulary undocumented (`semantic_fail` step → `silent_failure` run, unexplained).
- PRD FR-1.4: every finding carries `reason` text readable by someone who has never seen the node.
  `findings.py` already does this for the terminal line; extend to JSON.
- Do **not** add 1–5 quality scores to the dashboard. Sort by pass/fail + confidence instead.

---

## E-2 — Agentic pipelines need a *transition failure matrix*, not just an end verdict

**Pattern to use:** "Rows = last successful step, columns = step where failure occurred." Show
hotspots across runs, not one run at a time.

**What operators say:**
- "A single pass/fail judgment on the final outcome is a good start, but it is not diagnostic.
  When an agent fails, you need to know *which* step in the chain of reasoning broke … a
  transition failure matrix … shows you exactly where the assembly line breaks down most often."
  — Husain & Shankar, 2025-09-09 (url above)

**Field data:** `correlator.py` already computes `DegradationOrigin` per run;
`good-to-have-dev/flaky-tool-heatmap.md` notes "data is already captured, just not aggregated."

**Changes to the plan:** PRD US-4.6 "Failure hotspot matrix" — cross-run aggregation of
(origin node → failing node). Cheapest high-value dashboard addition; no LLM required.

---

## E-3 — Evals vs. production monitoring is a false dichotomy → ARGUS must be both a CI gate and a runtime observer

**What operators say:**
- "There's just this false dichotomy of either evals is going to solve everything or production
  monitoring is going to solve everything … evals are important, production monitoring is
  important, but this notion of only one of them is going to solve things for you is completely
  dismissible." — Kiriti Badam, *Aishwarya Naresh Reganti + Kiriti Badam* (podcast, 2026-01-11)
- "While CI protects you from 'known unknowns,' production is where you find the 'unknown
  unknowns.' Your production monitoring system is a discovery engine for new failure modes."
  — Husain & Shankar, 2025-09-09

**Field data:** ARGUS today = `pytest --argus` + `argus check` (CI side) and per-invoke run
records (runtime side). Missing: any exporter (OTel/webhook) to push runtime findings out, and
any way to promote a production failure into a regression fixture.

**Changes to the plan:**
- PRD §4.5 Integrations: OTel + webhook exporters are P1, not "nice to have."
- PRD US-2.7 `argus fixture add <run-id> --node X`: one command to turn a caught production
  failure into a committed regression fixture. Closes the loop the operators describe.

---

## E-4 — Measure the judge against humans (TPR/TNR), or don't trust it on a dashboard

**What operators say:**
- "By using the TPR and TNR you calculated for your judges, you can even statistically correct
  their [numbers on the dashboard]." — Husain & Shankar, 2025-09-09
- "Don't put off-the-shelf scores on a dashboard — sort examples by score and look at the best
  and worst." — same authors, podcast 2025-09-25

**Field data:** `signature_stats.py` + `feedback_store.py` already record disputes
(`argus stats … dispute`). No aggregate precision/recall per signature exists. The fault matrix
gives a one-off recall of **4/10** for heuristic-only mode.

**Changes to the plan:**
- PRD US-1.9: per-signature precision from dispute data, shown in `argus stats`.
- PRD §8: the recall matrix becomes a tracked metric with a target, re-run on every release.

---

## E-5 — Narrow persona first; community depth beats breadth for dev tools

**What operators say:**
- "They started with a really narrow, early focus. It was a single persona, single context,
  single use case … developers building applications using Node.js who wanted to ensure that
  the open source dependencies they were pulling in were secure." — Ben Williams (Snyk),
  *How Snyk built a product-led growth juggernaut* (2022-11-06)
  https://www.youtube.com/watch?v=21sFTZzIfUk
- "Starting with that narrow focus and building around community engagement … a well-proven
  playbook in the developer tooling space." — same.
- "Supabase's open source community shared docs, tutorials, and videos, further driving growth
  and building credibility." — *Ecosystem is the next big growth channel* (2025-11-11)
  https://www.lennysnewsletter.com/p/ecosystem-is-the-next-big-growth

**Changes to the plan:**
- PRD §2 persona: **LangGraph developer shipping a multi-node pipeline to production.** Adapter
  work for CrewAI/ADK/etc. stays *explicitly deferred* (matches `CONTRIBUTING.md`).
- PRD §6: contributor ladder (fixture → signature → detection rule → module owner). Fixtures are
  the Snyk-style "single low-friction action" for newcomers.

---

## E-6 — Activation: time-to-first-caught-failure is the aha moment

**What operators say:**
- "The speed at which a user can go from signing up to actually using the product
  (time-to-value) is critical … they quickly gave you templates to get started."
  — Hila Qu, *The ultimate guide to adding a PLG motion* (2023-04-02; summary 2024-08-12)
  https://www.lennysnewsletter.com/p/summary-the-ultimate-guide-to-adding-a-plg-motion--hila-qu-reforge-gitlab
- "Activation is usually a good place. If you don't know where to start, do that." — same.

**Field data:** `argus init` + skill files exist. The README's own troubleshooting line ("Empty
dashboard → wrong directory or no run yet") is the most common activation failure. The
stability review's first live pass caught a real refusal — "refusal detection is the killer
feature" — i.e., the aha moment is *seeing one real catch*.

**Changes to the plan:**
- PRD US-3.1 `argus demo`: ships a bundled broken pipeline so a new user sees a caught
  silent failure in <60 s without wiring their own graph.
- PRD §8 metric: **time from `pip install` to first non-clean run ≤ 5 min.**

---

## E-7 — PRDs still earn their keep for large, distributed groups

**What operators say:**
- "PRDs are great vehicles for getting a very large group of people aligned on a set of
  sources of truth about experience and set of goals … so that a big group of people can row
  in the same direction." — Dianne Penn (Anthropic), 2026-07-26
  https://www.lennysnewsletter.com/p/anthropics-first-technical-pm-on
- Counter-view: "70–80% of what we ship does not have a PRD … 20–30% where it's really
  important to get right, the documentation should be really good." — Amol Avasare
  (Anthropic growth), 2026-04-05
- "The PRD is the most important document if you want the AI to execute your vision correctly
  … whenever the AI hits a wall or the context window runs out, you can refer it back to the
  PRD to realign." — *How I built LennyRPG* (2026-03-17)
  https://www.lennysnewsletter.com/p/how-i-built-lennyrpg

**Changes to the plan:** This folder exists because ARGUS is (a) multi-contributor across
forks, (b) built heavily with AI coding agents, and (c) has an open-core boundary that must be
respected by every PR. Those are exactly the three cases operators say justify a written PRD.
Small fixes still don't need one — `CONTRIBUTING.md` labels cover those.

---

## E-8 — Developer experience: flaky tests and merge wait are the top-ranked friction

**What operators say:**
- "Flaky tests are rated the #1 priority from developers … 1,000 developer hours spent waiting
  to merge PRs." — *Introducing Core 4* (Laura Tacho / DX, 2025-01-14)
  https://www.lennysnewsletter.com/p/introducing-core-4-the-best-way-to-measure-and-improve-your-product-velocity

**Field data:** `tests/test_e2e_langgraph.py::TestCLI` fails on any machine where `python3` is
not the venv interpreter (reproduced on master during PR #48 review). CI runs three Python
versions serially per PR; no coverage gate; no `mypy` in CI despite `CLAUDE.md` listing it.

**Changes to the plan:** PRD §4.6 CI: fix the env-dependent test, add coverage floor, add
`mypy` job, publish `--format json` schema so downstream harnesses stop hand-parsing.

---

## E-9 — Dashboard: live, real-time, no massaged data

**What operators say:**
- "If we're looking at metrics, let's just look at your dashboard live. Let's not create a
  special presentation … that means you have to have a really good dashboard that's real time,
  that's web accessible. No one had to pull or massage the data." — Claire Hughes Johnson
  (Stripe), 2023-03-05 https://www.youtube.com/watch?v=Mv0o9o4MRh0
- "Many teams build eval dashboards that look useful but are ultimately ignored … because the
  metrics these evals report are disconnected from real user problems." — Husain & Shankar.

**Field data:** Sidebar ships five `soon` items (Traces, Evaluation, Graphs, Alerts, Datasets).
`EvaluationBuilder.tsx` renders a disabled "Evaluation (soon)" panel. Users see promised surfaces
that do nothing.

**Changes to the plan:**
- PRD US-4.1: remove `soon` nav items until each has a shipped backing feature (or hide behind a
  `?preview=1` flag). A dashboard that advertises five dead pages trains users to ignore it.
- PRD non-goal: no "quality score over time" chart until E-4 precision data exists.

---

## E-10 — Dashboard visual references (Mobbin, web, 2026-08-30)

### (a) Findings panel — US-4.2

**Pattern to use:** Group rows by severity with a coloured left rule and a count in the group
header ("Errors (3) / Warnings (4) / Notices (1)"); each row = plain-sentence description with
the affected item as a link, plus a "how to fix" link and an affected-count on the right.
← [Semrush Site Audit issues](https://mobbin.com/screens/f4c9575a-50af-462d-b267-bbe758587c33),
[HubSpot SEO recommendations](https://mobbin.com/screens/e9b536b6-aed8-4e62-b615-bde7c0428fe0)
(same idea, one card per issue with impact/difficulty chips and a "View pages" action).
Supabase's Security Advisor shows the three-column form (Issue type / Entity / Description) with
severity as top tabs — good when the list is long.
← [Supabase Security Advisor](https://mobbin.com/screens/e3ac7f2c-bdd0-42a4-ac13-76fb893fd06d)

**Patterns to avoid:** Repeating an identical red pill on every row with no explanation
(Rox "Enrichment failed" ×5) — the chip carries no diagnostic value.
← [Rox contacts](https://mobbin.com/screens/3618e8a0-cb1e-4006-a050-c73c9243ddd4).
Collapsed accordion per finding (Surfshark) hides the reason behind a click — ARGUS findings
must show the sentence inline.
← [Surfshark scan results](https://mobbin.com/screens/5ec81876-a4f9-4e1b-aa96-12cd466fb1e9)

**Changes to the plan (US-4.2):** group by severity with header counts (Semrush), not a flat
sorted list; keep `reason` inline; add a right-aligned "Fix prompt" action that opens `argus fix`
output (HubSpot's per-row action). Suppressed findings go to a greyed "Suppressed" group at the
bottom, like HubSpot's "Resolved issues".

### (b) Hotspot matrix — US-4.4

**Pattern to use:** Small labelled grid, one colour ramp, cell shows the raw count, empty cells
left blank (not zero-filled), legend "Low → High" above. Klaviyo's cohort grid is the closest:
row labels on the left, column labels along the bottom, single-hue intensity.
← [Klaviyo cohort heatmap](https://mobbin.com/screens/098b8697-cda8-498a-ae3a-922940c65874),
[Maze similarity matrix](https://mobbin.com/screens/74cb0049-6d2a-4024-a2f5-d80467d9f64a)
(sparse matrix with blank cells and a one-line explainer under the title — copy that explainer).

**Patterns to avoid:** Diverging red/green ramps (Steep, Zoho) — ARGUS counts are one-directional;
red/green implies good/bad on both ends. Treemap-style tiles (Kraken) — lose the row×column
reading.
← [Steep](https://mobbin.com/screens/4b8bbec1-fe24-48c7-83f2-cfd5d75cd2d2),
[Zoho CRM cohorts](https://mobbin.com/screens/e3827f36-4c70-434a-b2f3-c50801d324d5),
[Kraken heatmap](https://mobbin.com/screens/74246a23-9cbc-463f-8ad7-2b9ddd4e2fb4)

**Changes to the plan (US-4.4):** single-hue amber ramp (matches `silent_failure` colour); blank
cells for zero; one-line explainer under the title: "Rows = node that caused the failure, columns
= node where it surfaced"; row/column labels truncate with tooltip beyond 14 chars.

### (c) Run table with tag filters — US-4.3

**Pattern to use:** Active filters as removable chips in a row directly above the table, each
chip reading `key: value ×`, with a trailing "+ Add filter" affordance. Status as a coloured pill
in its own column.
← [Aboard approvals](https://mobbin.com/screens/43b940cb-28a4-42b5-9974-2073b895ad66),
[Twenty companies](https://mobbin.com/screens/c4089aab-6ec1-4507-b53f-19abd7dbf459)
(filter chips + multi-select dropdown for a field's values — use for `agent`, `suite` tags).

**Patterns to avoid:** Filter state hidden inside a dropdown only (Airtable, Workable) — user
can't see what's applied from the table.
← [Airtable](https://mobbin.com/screens/b3b21595-5794-4b20-bb85-ba48a59f12a1),
[Workable](https://mobbin.com/screens/197b7105-f9d9-4c78-a67b-9f5ecc212190)

**Changes to the plan (US-4.3):** chips row above `RunTable`; chip = `tag: value ×`; "+ Add
filter" opens a value multi-select per tag key; filter state mirrored in URL (already in the
story). Status stays the first column.

### (d) Empty state — US-4.5

**Pattern to use:** Two side-by-side CTAs — primary "connect real data", secondary "try demo
data" — with one sentence of context. Steep does exactly this ("Connect data source" /
"Try demo data"). Grok's console pairs the empty state with a ready-to-copy command block.
← [Steep get started](https://mobbin.com/screens/befce3e9-03ba-47d4-b4bc-5ecd3b647d8c),
[Grok console](https://mobbin.com/screens/8d063d6f-c0ef-4a38-931a-f6fd0c353397)

**Patterns to avoid:** Illustration-heavy "Get your first install" with a single marketing CTA
(Whop) — developers want the command, not the mascot.
← [Whop](https://mobbin.com/screens/6de0f2b2-772e-4d3f-8354-3b24feb3f690).
Panels that say "No data" with no path forward (PandaDoc production panel).
← [PandaDoc](https://mobbin.com/screens/788ff117-8786-4b9e-a168-ef25d94a0ac1)

**Changes to the plan (US-4.5):** primary = copyable `argus demo --open` block; secondary =
"Attach to my graph" linking to Guide; keep the existing "reading from `<path>`" diagnostic line
under both. No illustration.

## No evidence found for

- **Compare / diff view** layout (`app/compare/`) — not searched; out of this PRD's story list.
- **Open-source governance specifics** (CLA bots, CODEOWNERS granularity, release cadence) — the
  Lenny's archive is product/growth-oriented; searches for `maintainer|contributors|CLA` returned
  growth stories, not governance mechanics. PRD §6 relies on common OSS practice (Apache
  projects, CNCF sandbox templates) — stated as judgment, not sourced.
- **Pricing/packaging for open-core dev tools** — out of PRD scope (cloud/ is owner-only).
