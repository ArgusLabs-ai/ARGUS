# Market — gaps, positioning, and how to grow ARGUS

Companion to `PRD.md`. The PRD says *what to build*; this says *why anyone will care, who, and
how they find out*. Sources: Lenny's Newsletter / Podcast archive (searched 2026-08-30) plus the
repo's own field data. Every claim is either cited or marked **judgment**.

Baseline: 29★, 8 forks, 6 open issues, 30 releases, PyPI `argus-agents` 0.10.5, one live
consumer (the 50-agent lab). No pricing page, no public users list, no launch yet.

---

## 1. Positioning — fill April Dunford's five components, in order

Dunford's rule: start from **competitive alternatives** ("what customers would do if our solution
didn't exist"), then derive unique attributes → value → who cares most → market category. Teams
that start anywhere else "end up with positioning that sounded good in the office but didn't work
with customers."
— *Positioning* (April Dunford, 2021-01-26) https://www.lennysnewsletter.com/p/positioning
— *A guide to advanced B2B positioning* (Dunford, 2026-03-10): most weak positioning traces to
the team disagreeing on what to position *against*; sales rarely counts the **status quo** as a
competitor even when most deals are lost to it.
https://www.lennysnewsletter.com/p/a-guide-to-advanced-b2b-positioning

Applied to ARGUS (draft — needs the owner's sign-off, then test on 5 prospects):

| Component | Draft answer | Confidence |
|---|---|---|
| **1. Competitive alternatives** | (a) **Status quo: `print()` + reading logs + a `try/except` that swallows.** This is the real competitor. (b) Tracing/eval platforms — LangSmith, Braintrust, Arize Phoenix — named together by Hamel Husain & Shreya Shankar as the interchangeable "observability tool" tier ("we don't have a favorite tool"). (c) pytest + hand-written assertions. | High for (a); (b) is *horizon* competition per Dunford's product-team bias warning — they show traces, they don't rule on them |
| **2. Unique attributes** | Detects failures that *don't throw* (dropped field, `{}` no-op, placeholder, refusal) and blames the **origin node**, not the crash site. Works heuristic-only, offline, no account. `argus fix` emits a paste-ready prompt for a coding agent. Deterministic replay with state patching. CI gate (`pytest --argus`, `argus check`). | High — verified by the 50-agent review ("refusal detection is the killer feature") |
| **3. Value** | "Your pipeline said it succeeded. ARGUS tells you which node lied, before it ships." Time-to-root-cause drops from reading N logs to one terminal line. | Medium — no customer quotes yet |
| **4. Who cares most** | LangGraph developers with ≥4-node graphs going to production, especially those building with AI coding assistants (they can't read every node). Secondary: platform/QA engineers running agent fleets. | Medium — matches PRD persona P1/P2; needs 10 interviews |
| **5. Market category** | **Not** "LLM observability" (owned, crowded, implies dashboards). Candidate: **"production readiness for agent pipelines"** — already in `pyproject.toml`'s description. Frames ARGUS as a gate, like a linter/type-checker, not a monitor. | Judgment — test both framings |

**Gap G-1:** The README pitches the pain well ("three nodes later, `KeyError`") but never names
the alternative it replaces or the category. Add one sentence each.

---

## 2. Wedge — narrow on purpose

"A product that's 10x better on average might actually be 50x better for some groups but only
1.1x better for others. Picking the right group will determine whether you quickly find
product-market fit." — Leo Polovets, quoted in *Picking a wedge* (Lenny, 2021-10-26)
https://www.lennysnewsletter.com/p/picking-a-wedge

Good-wedge checklist from that post, scored for ARGUS:

| Criterion | ARGUS today | Gap |
|---|---|---|
| Narrow & focused | LangGraph only ✅ | Keep. Don't ship adapters yet (PRD N-2) |
| Builds momentum (sold fast, keeps users coming back) | `pytest --argus` = daily touchpoint ✅ | Needs `argus demo` for the first touch (US-2.6) |
| Extends into a bigger opportunity | Fleet-scale QA, exporters, hosted tier | Exporters not built (US-5.x) |
| Avoids competition | Silent-failure gating ≠ tracing ✅ | Say so explicitly (G-1) |
| Hard to replicate | 72 signatures + correlator + replay | Signature *precision* data would make this defensible (US-1.9) |
| Educates the market | "silent failure" vocabulary is ARGUS's | No content explaining the concept exists outside README (G-3) |

Snyk's playbook, same shape: "single persona, single context, single use case … Node.js
developers … open source dependencies … secure." Free from day one; ~5,000 free users before any
monetization; first 100 users from founders engaging the Node community directly.
— *How Snyk built a PLG juggernaut* (Ben Williams, 2022-11-06)
https://www.youtube.com/watch?v=21sFTZzIfUk

**Gap G-2:** ARGUS has no "first 100 users" motion. The LangGraph Discord/GitHub Discussions,
LangChain community calls, and r/LangChain are the equivalent of Snyk's Node meetups. Nobody is
there on ARGUS's behalf.

---

## 3. Channels — what worked for dev tools in the archive

- **Community + ecosystem before ads.** "Supabase's open source community shared docs, tutorials,
  and videos, further driving growth and building credibility"; Vercel's reach came from Next.js
  contributors. — *Ecosystem is the next big growth channel* (2025-11-11)
  https://www.lennysnewsletter.com/p/ecosystem-is-the-next-big-growth
- **Meetups, forums, open-source maintainers, dev thought leaders** were the first-user source
  for Snyk, Plaid, Databricks. — *How to find and win your first 10 B2B customers* (2023-09-05)
  https://www.lennysnewsletter.com/p/how-to-find-and-win-your-first-10-b2b-customers
- **Product Hunt: only when self-serve is smooth.** "If you're launching a B2B product, you need
  a smooth, self-serve onboarding flow … Delayed access? You've lost them." 50–120 h of prep for
  a good result; "founders often launch too late … launch early and often."
  — *How to successfully launch on Product Hunt* (2024-03-05)
  https://www.lennysnewsletter.com/p/how-to-successfully-launch-on-product-hunt-when-its-right-for-your-startup
  Gamma's warning: a PH spike "could have fooled ourselves into thinking we have product market
  fit." — Grant Lee (2025-11-13) https://www.youtube.com/watch?v=3H0ngGU5pbM

**Gap G-3 (content):** Zero external content. The 50-agent stability review and the fault matrix
are *already written* and are exactly the "look at your data" content Hamel/Shreya say builders
want. Publish them.

**Gap G-4 (launch readiness):** Self-serve is not smooth yet — no `argus demo`, dashboard shows
five dead pages, README links point at the old repo. Launch after PRD sequencing step 4.

Channel plan (judgment, ordered by cost):
1. Post the stability review + fault matrix as a blog/GitHub Discussion. Cross-post to LangChain
   community, r/LangChain, HN "Show HN" *after* G-4 is closed.
2. Answer LangGraph "my pipeline silently fails" threads with a 3-line ARGUS repro. Weekly.
3. Ask the 50-agent lab and any early user for a one-paragraph quote → README "Who uses it".
4. Product Hunt only once `argus demo` gives a <60 s aha. Budget 50 h prep.
5. A LangChain integration page / `langgraph` ecosystem listing (partner channel).

---

## 4. Measuring whether it's working — Sean Ellis + Superhuman engine

- **The 40 % test.** "How would you feel if you could no longer use this product?" ≥40 % "very
  disappointed" predicts growth; "way more predictive of success than NPS."
  — Rahul Vohra (2025-03-23) https://www.youtube.com/watch?v=0igjSRZyX-w;
  Sean Ellis (2024-09-05) https://www.youtube.com/watch?v=VjJ6xcv7e8s
- **The engine.** Ignore the "not disappointed"; ask the "very disappointed" *why*; keep only
  the "somewhat disappointed" for whom that same main benefit resonates; spend half the roadmap
  doubling down on the loved thing, half removing their objections. — Vohra, same source.
- **Stickiness ratio** (would-miss ÷ primary-use): "If more people would miss a tool than
  currently use it as their primary, that's a sign of strong product-market fit."
  — *AI tools are overdelivering* (2025-12-23)
  https://www.lennysnewsletter.com/p/ai-tools-are-overdelivering-results

**Gap G-5:** No user list, no survey, no way to run the test. Add `argus doctor --feedback` or a
one-question link in the README once ≥30 users exist. Until then, the proxy is GitHub issues
opened by non-maintainers per month (currently: ~1).

---

## 5. Open-core boundary — what stays open, what's paid

dbt's rule: "open source is really the guts … where you describe your business logic … what we
reserve for our proprietary offering is **state** — stateful interactions — and any **cross-team
or structural collaboration**." Also: "you don't get to decide if you're going to have a
willingness-to-pay conversation" — investors funding GitHub stars without revenue was a
zero-interest-rate artifact. — Julia Schottenstein (dbt Labs, 2023-07-13)
https://www.youtube.com/watch?v=y9hmrMBRPDI

Mapped onto ARGUS's existing split (`src/argus/` Apache-2.0; `cloud/`, `supabase/` proprietary):

| Stays open (the "guts") | Belongs in cloud (state + collaboration) |
|---|---|
| Detection rules, signatures, correlator, replay, CLI, local dashboard, BYOK judge | Cross-run history at fleet scale, team sharing/approvals, hosted LLM proxy, alerting, retention, SSO |

This is already the shape of the repo. **Gap G-6:** it isn't written down as a principle, so every
new feature triggers a "which side?" debate. Add the dbt rule to `CONTRIBUTING.md` under CLA.

Pricing pattern for bottom-up dev tools: freemium, then usage-based or flat + usage; nearly all
started self-serve and added sales later. — *How today's fastest-growing B2B startups turned
early users into paying customers* (2020-07-28)
https://www.lennysnewsletter.com/p/how-todays-fastest-growing-b2b-startups-turned-their-early-users-into-paying-cus
CFOs still want predictability; pure usage-based is hard to budget — Naomi Ionita (2023-01-12)
https://www.youtube.com/watch?v=xvQadImf568. **Owner decision; out of PRD scope.**

---

## 6. Product gaps the market research surfaced (beyond the PRD)

| # | Gap | Why it matters | Where it lands |
|---|---|---|---|
| G-1 | README never names the alternative or category | Dunford: undifferentiated = invisible | README, one PR |
| G-2 | No first-100-users motion in LangGraph community | Snyk/Plaid/Databricks all started there | Maintainer time, weekly |
| G-3 | No public content; the best content is already in the repo | Hamel/Shreya: builders trust "look at your data" posts | Publish stability review + fault matrix |
| G-4 | Self-serve not launch-ready | PH/HN punish delayed aha | PRD seq. step 4 (`argus demo`, kill `soon`) |
| G-5 | No PMF measurement | Can't run the 40 % test with no user list | After 30 users |
| G-6 | Open/paid boundary undocumented | Every feature re-litigates it | CONTRIBUTING one paragraph |
| G-7 | No "who uses it" / social proof | Wedge criterion 2 (momentum) | One quote from the lab, then more |
| G-8 | "Silent failure" concept not explained anywhere except the README hero | Wedge criterion 6 (educate the market) | One explainer doc + the fault taxonomy from `docs/STATUS.md` (US-1.1) |
| G-9 | Two maintainers, both owner-gated on `.github/` and `pyproject.toml` | Slows the release cadence that PLG needs | US-6.3 |

---

## 7. What to do this month (judgment, ordered)

1. Owner + co-maintainer fill the 5-component table in §1 together — 1 hour. Disagreement on
   row 1 is the thing to surface (Dunford's roadblock #1).
2. Rewrite README first screen: alternative named, category named, one user quote.
3. Publish `ARGUS_STABILITY_REVIEW.md` + fault matrix as a post. Link from README.
4. Ship `argus demo` and remove `soon` nav (PRD step 4). That's the launch gate.
5. Start the weekly LangGraph-community answer habit.
6. Only then: Show HN, then Product Hunt.

## No evidence found for

- Dev-tool-specific GTM playbooks (search returned one Atlassian result). Snyk/Supabase/Vercel
  stories are the closest.
- Open-core specifics beyond dbt (one hit). Rest is judgment.
- Anything on competing directly with LangSmith — the archive treats those tools as neutral
  infrastructure, not as a market to attack. Consistent with positioning *alongside* them.
