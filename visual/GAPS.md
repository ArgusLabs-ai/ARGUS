# Gaps — in the market, in the product, in the open-source setup

Three questions, answered separately:

- **A.** What is missing in the *market* ARGUS is building for (AI-agent reliability)?
- **B.** What do operators in Lenny's archive say is *necessary* that ARGUS doesn't have — or has, and should lean on to stabilize?
- **C.** What does a healthy open-source repo need, what does this one have, and where does the co-maintainer sit?

Sources: Lenny's archive (searched 2026-08-30), repo audit via `gh api`, and the 50-agent stability
review. Cited or marked **judgment**.

---

## A. Market gaps ARGUS can own

### A-1. Nobody gates "Category 2" agents; everyone traces them
The archive's own taxonomy: **Category 1** deterministic workflows (n8n, Zapier — "you define
the flow"), **Category 2** reasoning/acting agents (LangGraph, CrewAI, ADK — "you control the
tools; the LLM controls the reasoning"), **Category 3** multi-agent networks. Category 2 is
25–30 % of opportunities and "breaks in production because the tool is not robust enough."
— *Not all AI agents are created equal* (2026-04-14)
https://www.lennysnewsletter.com/p/not-all-ai-agents-are-created-equal

The metrics that post prescribes for a shipped agent — **workflow completion rate, automation
rate, error rate, human-review rate, latency P50/P95, cost per run** — are exactly what an
ARGUS run record already contains per node, but ARGUS never aggregates them into those names.

**Gap:** The market has a vocabulary for judging agents (completion / error / human-review rate)
and ARGUS speaks a different one (silent_failure / degraded_input). Map them. A one-screen
"agent scorecard" using the post's six metrics, computed from `.argus/runs/`, is a feature
nobody in the LangGraph ecosystem ships today. → **PRD candidate US-4.7.**

### A-2. "Look at your data" has no tool for pipelines
Hamel Husain & Shreya Shankar: "build your own error-analysis interfaces"; the workflow is open
coding (free-text critique + pass/fail per trace) → axial coding (cluster into <10 failure
modes) → count. They demo it in a spreadsheet + CSV export because no tool fits.
— podcast 2025-09-25 https://www.youtube.com/watch?v=BsWxPI9UM4c

**Gap:** ARGUS already stores every trace and already has `feedback_store.py` + `argus stats
dispute`. It is one step from being the annotation tool the eval community keeps hand-rolling:
a `note` field per node per run, exportable as CSV with `status`, `findings`, `note`. → **PRD
candidate US-2.8 `argus annotate <run> <node> "…"` + `argus export --csv`.**

### A-3. Transition-failure hotspots — already planned (US-4.4)
"You need to know *which* step in the chain of reasoning broke … a transition failure matrix."
— *Building eval systems* (2025-09-09). No LangGraph-native tool draws this. ARGUS has the data.

### A-4. Non-determinism is the unacknowledged input
"You're working with a non-deterministic API … pretty much black boxes." — Kiriti Badam
(2026-01-11). Same-input/different-output is normal, so a single failing run proves little.
**Gap:** no tool separates *persistent* failures (same node fails across N runs) from
*stochastic* ones. ARGUS's own stability review asked for this (P2 #9). → **PRD candidate:
flaky-failure tracking keyed by graph+node**, reusing `good-to-have-dev/flaky-tool-heatmap.md`.

### A-5. The "Challenger disaster" framing
Simon Willison: "every single time you get away with launching … without the O-rings failing,
you institutionally feel more confident … We've been using these systems in increasingly unsafe
ways." (2026-04-02). He coined *prompt injection*; guardrail models "aren't actually very good."
— Sander Schulhoff (2025-06-19), Reganti & Badam (2026-01-11).

**Gap:** the market's fear is *silent* compounding failure, not loud crashes — precisely
ARGUS's pitch. Nobody is selling "the O-ring check." Use that language in positioning
(MARKET.md §1). Indirect-injection testing (`good-to-have-dev/indirect-injection-testing.md`)
is the strongest differentiator on this axis; keep it parked until the core stabilizes, but
name it on the roadmap.

### A-6. Guardrails belong *where the model breaks*, and someone has to find that spot
"Where it starts to go wrong is exactly where you need to design guardrails … A good guardrail
determines what the product should do **when the model hits its limits**."
— *Building AI product sense, part 2* (2026-02-10)
https://www.lennysnewsletter.com/p/building-ai-product-sense-part-2

**Gap:** teams find the break point by hand. ARGUS's `validators={...}` are guardrails; the
hotspot matrix tells you where to put them. Docs should say that sentence — today validators are
presented as a config option, not as "the guardrail you add at the node ARGUS just flagged."

---

## B. What Lenny's says is necessary — audit against ARGUS

| Necessary (source) | ARGUS has | Missing / stabilizer |
|---|---|---|
| Reference dataset of 20–100 examples before building; evals against it; expect to rebuild evals when production differs (*AI dev lifecycle*, 2025-08-19) | `fixtures/` (1 class), fault matrix (10 cases) | Fixtures aren't run by tests (US-3.1). Fault matrix lives outside the repo (`~/argus-agents-lab`) — **bring `faults/` into `tests/` so recall is a CI number** |
| Binary judge + critique; measure judge vs. humans (Husain/Shankar) | `semantic_checker` returns pass + reason + `evidence_considered` | No TPR/TNR for the judge; no per-signature precision (US-1.9) |
| Logs capture what the system saw, returned, and how people interacted (*AI dev lifecycle*) | `NodeEvent.input_state / output_dict` | "How people interacted" = disputes/annotations; annotation absent (A-2) |
| Production monitoring as "discovery engine for new failure modes" (Husain/Shankar) | per-invoke run records | No exporter; findings never leave `.argus/` (US-5.1) |
| Implicit signals — regenerate = thumbs-down (Badam) | `retried` status | Retry-after-fail is captured but not surfaced as a health metric (A-1 scorecard) |
| PMF measured by the 40 % test; iterate on "somewhat disappointed who love the main benefit" (Vohra) | — | No user list (MARKET G-5) |
| "Ship like a startup": prototypes over PRDs, evergreen visual artifacts (*How to ship like a startup*, 2025-03-25) | this `visual/` folder; `TRIAL_UI_SPEC.md` | **Judgment:** keep PRD stories small (they are); add one animated GIF of `argus demo` to README once it exists — the "evergreen artifact" |
| Flaky tests are developers' #1 friction (*Core 4*, 2025-01-14) | CI on 3 Pythons | 2 env-dependent failures (US-6.1) |
| Changelog per change, not per release (Claude Code changelog example, 2025-10-14) | `CHANGELOG.md` maintained | Written at release time by owner → **move to per-PR entries** (US-6.3) |
| Guardrail at the break point (*Product sense, part 2*) | `validators`, `strict` (planned) | Docs framing (A-6) |

**Stabilizers already in hand (lean on these):**
1. The 50-agent test bed — nobody else in the ecosystem has 50 real graphs under CI. Make it a
   public benchmark (`ARGUS_STABILITY_REVIEW.md` → blog + `faults/` in-repo).
2. `argus fix` — turns a finding into a paste-ready coding-agent prompt. Unique; under-marketed.
3. Heuristic-only mode — works with no key, no account. Every competitor needs a login.
4. `good-to-have-dev/` decision records — a maturity signal most 30-star repos lack.

---

## C. Open-source collaboration — what's necessary, what exists, where you sit

Lenny's archive is thin on OSS governance mechanics (searched: `maintainer|contributors|CLA|
governance` → growth stories only). This section is **judgment from common practice** (CNCF /
Apache / GitHub community-standards checklist), checked against the live repo on 2026-08-30.

### C-1. Repo audit

| Item | Status | Why it matters | Action |
|---|---|---|---|
| LICENSE (Apache-2.0 core) | ✅ but GitHub shows `NOASSERTION` — dual-license confuses the detector | Badges, dependency scanners, and legal reviewers read that field | Add `LICENSE` header comment pointing to `cloud/LICENSE`; or split into `LICENSE` + `LICENSE-cloud` — **owner** |
| README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, CLA | ✅ | GitHub "community standards" green | Fix stale links (`VaradDurge/ARGUS` → `ArgusLabs-ai/ARGUS`) |
| Issue templates (bug, feature) | ✅ | | Add fixture + signature templates (US-3.3) |
| PR template + labels | ✅ 15 labels | | Add `needs-fixture`, `release-request` |
| CODEOWNERS | ✅ two maintainers, owner-only paths | | Path-level ownership (US-3.4) |
| Branch protection on `master` | CONTRIBUTING says yes; API returns 404 (no admin visibility) | Contributors can't verify the rule they're told | Owner confirms; add a status badge or note |
| CI (ruff + pytest) | ✅ | | + mypy, coverage floor (US-6.2) |
| Release automation (tag → PyPI) | ✅ `publish.yml`, 30 releases | Release cadence is healthy | Document the cadence (US-6.3) |
| **GitHub Discussions** | ❌ disabled | Only channel for "is this a bug or am I holding it wrong?" is Issues → noise | **Enable; pin "Show us your silent failure" thread** — owner, 2 min |
| **Dependabot / Renovate** | ❌ | 3 runtime deps + Node dashboard; unpatched CVEs are the #1 OSS trust killer | `.github/dependabot.yml` (pip + npm, weekly) — owner |
| **GOVERNANCE.md / maintainer ladder** | ❌ | Two people, both owner-gated on `.github/`; no written path from contributor → maintainer | Fold into CONTRIBUTING "Your first four PRs" (US-3.2) + a "How maintainers are added" paragraph |
| **Public roadmap** | ❌ (lives in `good-to-have-dev/` + now `visual/`) | Contributors pick work blind | Link `visual/PRD.md` §10 from README; GitHub Project board mirrors it |
| `good first issue` | 3 open, oldest #2 from the start | Newcomers' entry point | Keep ≥5 open; each must have a file pointer + test hint |
| **Issue response time** | Issue #46 (external) has **0 comments** | First-response time is the metric contributors watch | 48 h first-response rule; co-maintainer owns triage |
| Pre-commit config | ❌ | Contributors hit ruff failures in CI instead of locally | `.pre-commit-config.yaml` with ruff — any maintainer |
| FUNDING.yml | ❌ | Optional | Skip until there's something to fund |
| Star velocity | 1/mo → 14 (Jul) → 12 (Aug) | Growth started 2 months ago; the window for community habits is now | See MARKET.md §3 |
| External PR authors | 5 people, 9 PRs, 1 from a fork | Real but small | Every external PR gets a review within 72 h |

### C-2. Rules that make multi-person OSS work (judgment, ordered by impact)

1. **Respond before you review.** A one-line "seen, will review by Thursday" within 48 h beats a
   perfect review in two weeks. Issue #46 is the counter-example.
2. **Every PR moves the docs.** Already in CONTRIBUTING; make CI enforce it for `src/argus/*.py`
   adds/removes (grep `CLAUDE.md` key-files table).
3. **Fixtures are the currency.** A contributor's first win must be visible in CI (US-3.1).
4. **Small PRs, one concern.** "How We Work" already says this — it's the project's best rule.
   Enforce it in review; split, don't merge, when a PR does two things.
5. **Owner-gated paths batched.** Anything touching `.github/`, `pyproject.toml`, `LICENSE`
   waits for a weekly owner slot rather than blocking a feature PR.
6. **Public decisions.** Anything decided in chat goes into `good-to-have-dev/`,
   `visual/PRD.md` Open Questions, or a Discussion thread — never only in chat.

### C-3. Your position (Abhishek), read against C-1/C-2

- **Formal:** co-maintainer; default CODEOWNER; `push` + `triage`; **not** admin/maintain — you
  cannot enable Discussions, add Dependabot, edit branch protection, or change CI. Those five
  items above are owner-only asks; bundle them into one request.
- **Actual footprint:** 13 PRs (12 merged), #2 contributor, author of the stability review, the
  fault matrix, the process rule, and now the PRD pack. You own the *evidence* and the *process*;
  the owner owns the *core detection code* and the *release button*.
- **Natural role in the ladder:** community/triage maintainer. Concretely: 48 h first-response
  on every external issue/PR, keep ≥5 `good first issue`s live with file pointers, review fixture
  and signature PRs (they're small and match your test-bed knowledge), run the monthly
  `faults/` recall number.
- **What to ask the owner for:** (a) `maintain` permission — it unlocks Discussions, labels,
  branch protection *without* admin; (b) path-level CODEOWNERS for replay/state-patch/llm-proxy
  so those PRs don't need two approvals; (c) a written release-request path so you can ship a
  patch when the owner is out.
- **Gap to close on your side:** unify the two git identities (`.mailmap`), and move the
  `faults/` harness from `~/argus-agents-lab` into the repo so the evidence you own is
  reproducible by others.

### No evidence found for
- OSS governance from Lenny's (all of C is judgment). If you want sourced OSS practice, the
  places to look are the CNCF project template, GitHub's "community standards" checklist, and
  Nadia Eghbal's *Working in Public* — outside this archive.
