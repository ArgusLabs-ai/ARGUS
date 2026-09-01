# visual/ — ARGUS platform PRD & evidence pack

This folder is the single source of truth for **what ARGUS is becoming and why**. It exists so
that any contributor — human or AI coding agent — can pick up a piece of work without missing
context, dependencies, or the evidence behind a decision.

| File | What it is | Read it when |
|---|---|---|
| [PRD.md](PRD.md) | Master product requirements document. Goals, personas, user stories, functional requirements, non-goals, success metrics, open questions. | Before starting any feature, fix, or doc change. |
| [DEPENDENCY-MAP.md](DEPENDENCY-MAP.md) | How every workstream connects: what blocks what, which modules each touches, which docs must change together. | Before picking an issue, so you don't build on something that isn't there yet. |
| [EVIDENCE.md](EVIDENCE.md) | Research notes behind the product decisions — operator interviews (Lenny's Newsletter/Podcast), the 50-agent stability review, and what we could *not* find evidence for. | When a PRD requirement looks arbitrary and you want to know why. |
| [MARKET.md](MARKET.md) | Gaps, positioning (Dunford five-component draft), wedge, channels, PMF measurement, open-core boundary, month-one actions. | Before writing README copy, planning a launch, or deciding what's open vs paid. |
| [GAPS.md](GAPS.md) | Market gaps ARGUS can own, what Lenny's says is necessary vs. what exists, open-source repo audit + maintainer ladder + co-maintainer's position. | When choosing what to build next or asking the owner for repo settings. |
| [STATUS-abhishek.md](STATUS-abhishek.md) | Co-maintainer's current standing in the repo: merged PRs, owned areas, review rights, what's parked. | Onboarding a new contributor; deciding who reviews what. |

## How this folder relates to the rest of the repo

```
README.md               ← user-facing pitch + quick start (marketing surface)
CONTRIBUTING.md         ← how to contribute (process rules, labels, CLA)
CLAUDE.md               ← architecture map for AI coding agents
good-to-have-dev/       ← parked ideas with decision records
ARGUS_STABILITY_REVIEW.md ← field evidence from a 50-agent test bed
visual/PRD.md           ← THIS: the plan that ties all of the above together
```

Rule from `CONTRIBUTING.md` still applies here: **agreeing a plan is not agreement to execute all of
it at once.** Every user story in the PRD is sized to land as one reviewable PR.

## Updating this folder

- A PR that ships a user story ticks its acceptance boxes in `PRD.md` and links the PR.
- A PR that changes a decision updates `EVIDENCE.md` with the new reasoning (or says "no evidence, judgment call").
- If a story turns out bigger than one PR, split it in `PRD.md` first — don't push through.
