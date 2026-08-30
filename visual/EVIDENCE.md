# Evidence — why the PRD says what it says

Format follows the `visual-and-research` habit: look at what strong teams do *before* building.
Two source classes were used:

1. **Operator interviews & essays** — Lenny's Newsletter / Lenny's Podcast archive (MCP search,
   2026-08-30). Cited as *Title (who, date) url*.
2. **Field data from this repo** — `ARGUS_STABILITY_REVIEW.md` (50 LangGraph agents, 150 nodes),
   fault-injection recall matrix, open issues, and the codebase itself.

> **Visual references gap.** The Mobbin MCP (real app screens) was not connected in the session
> that produced this pack, so there are **no screenshot references** for dashboard layouts. Every
> UI requirement below is stated in words and marked `[needs Mobbin pass]`. Run
> `/visual-and-research` again with Mobbin connected before building any dashboard page and
> paste the screen links into the matching section here.

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

## No evidence found for

- **Screen-level layout references** for run-list / run-detail / compare views — Mobbin not
  connected. `[needs Mobbin pass]`.
- **Open-source governance specifics** (CLA bots, CODEOWNERS granularity, release cadence) — the
  Lenny's archive is product/growth-oriented; searches for `maintainer|contributors|CLA` returned
  growth stories, not governance mechanics. PRD §6 relies on common OSS practice (Apache
  projects, CNCF sandbox templates) — stated as judgment, not sourced.
- **Pricing/packaging for open-core dev tools** — out of PRD scope (cloud/ is owner-only).
