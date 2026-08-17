# B3 — is the migration inventory live, and is it complete?

Two questions the tracker skips, because §2 calls its six-site list "verified"
and B3 goes straight to picking one to pilot:

> Of the modules listed, which can actually run? And is the list complete?

**Answer: neither holds. There are seven LLM call sites, not six. Three of
them — `llm_correlator.py`, `heuristic_disambiguator.py`, and the one the
tracker misses, `loop_analyzer.py` — are unreachable. Nothing in the package
imports them, from either entry point ARGUS ships.**

That matters immediately, because `heuristic_disambiguator.py` is what
[FINDINGS.md](FINDINGS.md) recommended piloting BAML on. **That
recommendation was wrong and is withdrawn here.** A pilot measures a
migration against real traffic; against a module that never executes it
measures nothing.

Reproduce with:

```
python scripts/baml_spike/check_inventory.py --verbose
```

No dependencies, no keys, no network — import edges are read with `ast`
rather than by importing anything. Call sites are discovered by scanning for
`create_chat_completion(...)` and `...chat.completions.create(...)`, then
diffed against §2, so a site the tracker missed surfaces as a finding instead
of staying invisible.

> This continues the B1 transport-fit spike in `FINDINGS.md`, and retracts one
> of its recommendations. It needs no BAML install to run — reachability is a
> property of ARGUS's own source, not of anything BAML does.

---

## Results

```
package modules   : 54
reachable         : 51
entry points      : argus, argus.cli.main
LLM call sites    : 7 discovered, 6 in tracker §2

module                             calls   import       transport       §2
----------------------------------------------------------------------------------
argus.heuristic_disambiguator      1x      UNREACHABLE  shared proxy    yes
argus.llm_correlator               1x      UNREACHABLE  shared proxy    yes
argus.llm_investigator             3x      reachable    shared proxy    yes
argus.loop_analyzer                1x      UNREACHABLE  shared proxy    MISSING
argus.semantic_checker             1x      reachable    shared proxy    yes
argus.signature_generalizer        1x      reachable    shared proxy    yes
argus.source_locator               1x      reachable    shared proxy    yes
```

Reachability is walked from the two entry points the distribution exposes —
`argus` for library users, `argus.cli.main` for the `argus` console script,
per `[project.scripts]`. Each live site's shortest import path is recorded:

| Site | Reached via | LLM call at |
|---|---|---|
| `llm_investigator` | `argus` → `session` | `:734`, `:893`, `:1050` |
| `semantic_checker` | `argus` → `session` | `:281` |
| `source_locator` | `cli.main` → `cmd_open_ui` | `:399` |
| `signature_generalizer` | `argus` → `session` | `:202` |
| `llm_correlator` | — nothing imports it | `:157` |
| `heuristic_disambiguator` | — nothing imports it | `:113` |
| `loop_analyzer` | — nothing imports it | `:146` |

One line worth reading twice: **every unreachable module in the package is an
LLM call site.** There are exactly three, and all three are on this list. The
dead code in ARGUS is not scattered — it is concentrated entirely in the area
the tracker is about to migrate.

---

## The site the tracker missed

`loop_analyzer.py` (201 lines) calls the shared proxy with
`response_format={"type": "json_object"}`, `json.loads`-es the reply, and
defaults every field on a parse failure — the same shape as the other six. Its
module docstring opens:

> Mandatory, always-on analysis for looped nodes.

It is never called. `analyze_loops()` is referenced from
`tests/test_smoke.py` and nowhere else in the package.

The plumbing around it is half-built, which is what makes this easy to miss:
`RunRecord` carries a `loop_analyses` field (`models.py:274`) and `storage.py`
has a deserializer for it (`:334`, `:363`, `:498`), so persisted runs will
read the field back. Nothing ever writes one, so it is empty on every run.

It also carries the bug class fixed in the verdict PR, dormant:

```python
content = result.get("choices", [{}])[0].get("message", {}).get("content", "{}")
try:
    data = json.loads(content)
except json.JSONDecodeError:
    data = {}
```

An empty completion or unparseable reply becomes `data = {}`, which becomes a
`LoopAnalysisResult` with `summary=""`, `is_stalled=False`,
`unnecessary_retries=0` and **`error=None`** — a clean analysis, reported as
if the judge had run. Harmless today because the code never executes. Worth
knowing before anything wires it back in.

---

## Why dead modules ended up on a verified list

Both of the listed ones have a live near-namesake doing the job their name
suggests.

- **`llm_correlator.py`** (207 lines, no tests) vs **`correlator.py`** — the
  live one, imported at `session.py:1363`, 87% covered per
  `tests/AUDIT_REPORT.md`. `tests/test_correlator_unit.py` tests the live one.
  The dead one has no test file at all.
- **`heuristic_disambiguator.py`** (159 lines, 7 tests) vs the disambiguation
  folded into `semantic_checker.check_semantic_coherence`, whose docstring
  says it checks coherence and disambiguates "in one LLM call". That merge is
  what made the standalone module redundant; it was never removed.

`heuristic_disambiguator` still has passing tests, which is why a
test-suite-based inventory would not catch it. Its 7 tests exercise the module
directly, so they stay green while nothing else calls it. Coverage has the
same blind spot — `tests/AUDIT_REPORT.md` records it at 93%.

None of the three appear in `README.md`, `docs/`, or `website/`, and none is
exported from `argus/__init__.py`.

---

## What this changes in the tracker

**§2 — the inventory is wrong in both directions.** It overcounts live sites
by two and misses a seventh entirely. Seven sites exist; four run. The
migration surface is roughly half what the tracker scoped, before any of the
P9 provider multiplication in FINDINGS.md is applied.

**§4 — half the timeout table is dead.** §4 lists four per-call timeouts that
"must be preserved individually, not replaced with one generic BAML
retry/timeout policy". Against the source:

| Timeout | Site | Live? |
|---|---|---|
| 5.0s / 8.0s | `semantic_checker` (`8.0 if amb else 5.0`) | ✅ |
| 8.0s | `heuristic_disambiguator` | ❌ dead |
| 15.0s | `llm_correlator` | ❌ dead |
| 15.0s | `signature_generalizer` (added by B2, #18) | ✅ |
| 30.0s default | `llm_investigator`, `source_locator` | ✅ |

Two live sites pass an explicit `timeout=`: `semantic_checker` and
`signature_generalizer`. The other two take the 30.0s default. Neither of the
tracker's own overrides — the 8.0s and the 15.0s — is on live code; the one
live 15.0s is a different site that happens to share the number.

So P7's `threading.Timer` + `AbortController` wrapper has to reproduce two
explicit overrides plus a default, not the per-site matrix §4 implies. Smaller
than four, larger than it looked before #18.

> **This row moved.** Measured before #18, `signature_generalizer` was on a
> direct OpenAI client with no timeout at all, and this section said
> `semantic_checker` was the only live site passing one. #18 put it on the
> shared proxy and gave it a 15.0s bound, which is why the table above differs
> from the version first published here. `check_inventory.py` reads transport
> from imports, so its output tracked the change on its own; this prose did
> not, and was corrected by hand.

**§5 / B3 — the recommended pilot is off the table**, and so is the fallback,
since §5's two candidates were `semantic_checker` and
`heuristic_disambiguator`.

---

## B3, re-answered over live sites only

Ranked on what a pilot would actually demonstrate, using P6/P7/P8 from
FINDINGS.md:

**1. `semantic_checker.py` — the safe pilot, and now the only small-schema
live site.** 3-field schema, hot path, blast radius limited to one node's
status. The P6 objection stands and has grown: its parsing was hardened again
in the verdict fix, so BAML's parser has even less left to beat. A pilot here
proves the integration works without proving it is worth having.

**2. `llm_investigator.py` — where the parsing win is actually visible.**
Biggest schema by far (nested hypotheses, suggested signatures, suggestions),
three call sites rather than one, strips code fences by hand but has no
truncation repair, and its `_extract_*` helpers default every missing key —
`str(h.get("hypothesis", ""))`, `float(h.get("confidence", 0.0))` — which is
exactly the class of silent defaulting BAML's validation rejects. It is also
the largest blast radius of the four: its output is shared across a whole run,
not one node.

**3. `source_locator.py` — live, on bare `json.loads`, but a poor schema
fit.** Its target is `dict[str, str]` keyed by node name, so the keys are
runtime data. A declared schema buys little where the shape is a map with
arbitrary keys.

**4. `signature_generalizer.py` — not a candidate.** It returns a raw regex
string, not JSON. There is no schema to align. (B2/#18 moved it onto the shared
proxy path; that was a transport fix, unrelated to parsing.)

**Suggestion: `semantic_checker` for a low-risk integration proof, or
`llm_investigator` if the pilot is meant to answer whether BAML earns its
place.** Those answer different questions, and the tracker has not said which
one it is asking — that is the call to make before writing any BAML code.

---

## Not decided here

**What to do with the three dead modules.** Deleting 567 lines plus a test
file is a maintainer decision, not a spike's. Briefly:

- **Delete them.** Undocumented, unexported, unreferenced. Clears them from
  the migration surface permanently.
- **Keep and mark.** If any is staged for future wiring, a line in the module
  docstring saying so costs nothing and stops the next inventory from
  re-counting them.
- **Wire one back in.** `loop_analyzer` is the interesting case — its storage
  and model plumbing already exist, and "mandatory, always-on" reads like
  intent that was never finished. That is a detection-quality decision, not a
  BAML one. `heuristic_disambiguator` would mean regretting the one-call merge
  into `semantic_checker`.

Doing nothing is also a choice, but it means the next person scoping work
across "the six LLM sites" repeats this measurement.

---

## Limits of the method

- Static reachability proves the **negative** soundly: a module no code
  imports cannot execute. It does not prove a reachable site's LLM call fires
  on any given run — the live sites sit behind `is_available()` and config
  gates.
- A library consumer can still `import argus.loop_analyzer` directly. Nothing
  in the package does, and nothing documents it, but the import is legal —
  worth weighing before deleting.
- Call-site discovery matches two call shapes. A site reaching a provider by
  some third route would be missed; none exists today, and the §2 diff would
  flag the reverse case (a listed module with no LLM call).
- The graph counts function-local imports as edges, which ARGUS uses
  throughout to keep optional paths cheap. Ignoring them would report most of
  the package as dead.
- Ancestor packages count as reachable: importing `argus.utils.serializer`
  executes `argus/utils/__init__.py`. Omitting that reports `argus.utils` as
  dead, which is wrong.
