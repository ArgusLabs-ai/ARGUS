# Pivot branch — contributor update

Branch: **`pivot/fat-traces`**  
Last updated: 12 Sep 2026 (second update: silent-failure stress matrix + six detection fixes).

Same product: silent failures, origin blame, CI gate (`argus check`).  
Different capture: fat traces → ledger (notebook) → rules → judge last. No wrapping the graph engine.

**Do not merge wrap deletion into `master`.** The first PR into `master` is additive: `ArgusRecorder` sits next to `ArgusWatcher`.

---

## What we shipped on this branch

The loop a user can run today:

```text
ArgusRecorder.attach(app)     # listen; do not patch compile / invoke
  → fat trace (node, input, what the node returned, tools, errors)
  → ledger (notebook from the saved run file)
  → rules (contextual + inspector + signatures — not an LLM)
  → LLM judge last (on if a key exists, cannot override a critical)
  → argus check               # the verdict
  → optional: replay one failed node from the notebook + your app
```

```python
from argus import ArgusRecorder

app = ArgusRecorder(consumers={"docs": ["summarize"]}).attach(app)
app.invoke({"query": "..."})
# argus check last
# argus replay <id> rerank --only --app my_graph:build
```

| What | In plain language |
|---|---|
| **Fat recorder** | We listen to LangGraph’s own callbacks. We keep the dict the node **returned**, not the merged state. That is how `{}` is visible. No `patch_graph`. |
| **Ledger** | One notebook row per step that actually ran: input, update, state after, tools, error, status. Skipped branches are not rows. Save + reload matches. |
| **Contextual contracts** | You declare who reads a field later (`consumers={"sources": ["draft"]}`). We check **when the reader runs**, not at step 0. “Not written yet” is not a failure. Never written / dropped / written-empty still fail on the origin, not the victim. |
| **Silent `{}`** | A node that returns nothing while successors wait fails `argus check` on **that** node. |
| **Inherited emptiness** | A node handed `[]` that returns `[]` is a warning, not the origin. A node that **had** docs and dropped them is still critical. A retriever that finds nothing is still critical (product: empty search is a silent failure). |
| **Replay (new)** | Re-score is `argus check` (file only). Live rerun of **one** node: input from the ledger row, function from the app you pass in. No app → refuse, point at `argus check`. Original notebook is not rewritten. Upstream passers stay frozen in the notes. |

Proof (run these, not the full suite — embeddings make it slow):

```bash
PYTHONPATH=src python demo/fat_trace/demo_graph.py
argus check last
# graph “succeeds”; ARGUS fails summarize / empty_output

PYTHONPATH=src pytest tests/test_recorder.py tests/test_ledger.py \
  tests/test_ledger_unit.py tests/test_ledger_fidelity.py \
  tests/test_contextual.py tests/test_judge_last.py \
  tests/test_replay_from_ledger.py tests/test_rerun_e2e.py \
  tests/test_new_user_pipelines.py tests/test_inspector_unit.py \
  tests/test_silent_failure_matrix.py -q
```

End-to-end story in `tests/test_rerun_e2e.py`: ingest → retrieve → rerank → summarize → answer. Rerank drops every doc; the graph still answers. Check blames rerank. Replay feeds that row’s input (the 3 docs) into the fixed function; old output stays `[]`, new output has docs.

---

## Detection stress matrix (new) — and the six gaps it found

`tests/test_silent_failure_matrix.py` is the answer to "does this architecture
actually catch what enterprises ship?". 28 tests over seven real LangGraph
pipelines — supervisor/worker loop (cyclic + conditional), map-reduce fan-out
with `operator.add`, a four-node CRM triage chain, a tool-calling fetcher, a
degraded-output generator, a crash handoff, and a subgraph. `patch_graph` is
monkeypatched to raise in every test, so a slide back to the wrap path fails
loudly.

Every test asserts **the blamed node**, not just "something was found". A test
that only checks `passed is False` would also pass on the old behaviour, which
blamed the crash site.

Six tests started as `xfail(strict=True)`. All six are now fixed:

| # | What was missed | Why | Fix |
|---|---|---|---|
| 1 | A worker returning `{}` on **every** round graded **clean** — if it also owned the loop edge | `_get_successor_fns` returns `[]` for any conditional source, and `empty_output` was gated on that list. The exemption is right for the structural field check (you cannot require every branch's annotations at once) and wrong for `empty_output`, which never reads annotations | `inspect_transition(..., has_successors=)`, fed from `graph_edge_map`. A router is still a node |
| 2 | A silent node **inside a subgraph** graded clean | `attach()` read `get_graph()`, so a subgraph was one opaque `child` node and its inner nodes were absent from `node_fn_registry` — no successors, so exempt from (1) | `recorder._topology()` reads `get_graph(xray=True)` and strips the `parent:` prefix, since callbacks report bare names |
| 3 | Both of the above **double-missed** | `contextual._blame` stays quiet when any earlier row has `update == {}`, deferring to an `empty_output` that never fired | No code change. With 1 and 2 fixed the deferral is correct — and now tested |
| 4 | A `KeyError` crash blamed only the crash site | `inspector`'s phase-1 crash walk **already found** the origin; the correlator then overwrote `root_cause_chain` with its own answer. The correlator diffs input→output, so it can only nominate nodes that produced output — never the one that quietly omitted a field | Phase 1 extracted as `inspector.crash_origins()`; `session._blame_crash_origins()` marks the origin so `argus check` names it; the correlator no longer overrides on `crashed` |
| 5 | `answer: "N/A"` and `answer: "TODO"` raised findings but graded **clean** | Those signatures are `warning` severity — correct when they match inside a longer body, wrong when the placeholder *is* the whole answer | Promote to critical when the field is a main output key and its entire value is a short token (`_is_the_whole_answer`) |
| 6 | Lorem ipsum undetected | RF-005 requires `(?:lorem ipsum\s*){2,}` — two repeats | Added RF-006 for a single occurrence |

Also fixed (was listed under *Cosmetic*): a `missing_field` finding now keeps
the sentence contextual/the crash walk authored — the one that names the
**reader** — instead of `collect_findings` re-deriving a blander line, and
`origin_node` is set rather than `None`.

```bash
PYTHONPATH=src pytest tests/test_silent_failure_matrix.py -q   # 28 passed, ~20s
```

### Behaviour changes — read this before you debug a "broken" test

Two existing tests were updated because the **product** changed, not to make a
run green. If you have a branch asserting either of these, it will fail:

- **A crash no longer reports the crash site as the first failure.**
  `first_failure_step` and `root_cause_chain[0]` are now the node that *omitted*
  the field. `tests/test_recorder.py::test_a_crash_is_recorded_and_fails_the_gate`
  asserted `first_failure_step == "boom"`; it is now `"search"` — the node that
  ran before `boom` without writing the key `boom` died on. The crashed step is
  still recorded as `crashed` where it happened; only the blame moved.
- **A whole-value placeholder fails on the rules, not on the judge.**
  `{"answer": "I don't know"}` used to land as `semantic_fail` (only the LLM's
  verdict failed it) and is now `fail` with `has_tool_failure`, with or without
  a key configured. That is judge-last working as intended. Judge-authored
  `semantic_fail` is still covered by `test_async_judge_applies_fail_verdict`.

Ordering matters in `session._finalize`: `_blame_crash_origins()` runs **after**
`overall_status` is decided (so a crashed run stays `crashed`) and **before**
`first_failure` is computed (so the origin, not the victim, leads the report).

### What the matrix confirms already works

- Long-range contextual blame: `enrich` nulls `customer_id`, `analyze` runs in
  between, `respond` reads it three steps later → **`enrich` alone** is blamed.
- Fan-out isolation: one silent branch is blamed; the healthy sibling and the
  reducer are not.
- Swallowed tool failures: an HTTP 500 payload and a caught `RuntimeError` both
  fail the gate on the fetcher, with tool I/O on the right ledger row.
- The false-positive line: `vulnerabilities: []` with **no declared reader** is
  clean (a scan that finds nothing is a real answer); the same `[]` **under a
  declared consumer** fails. Emptiness alone is not the signal — that is the
  distinction to preserve in any future change here.
- Progressive state fill, unchosen conditional branches, read-write accumulator
  nodes, and `ainvoke` all behave.
- A skinny trace (node spans sampled away) raises `IncompleteTraceError`
  instead of "no findings, so clean".

---

## Still to fix (open issues)

These are known. They do **not** block pushing this branch. They **do** block deleting the old wrap and merging a “replacement” PR into `master`.

### 1. Replay does not continue the graph after the fixed node

You can rerun `rerank` from the notebook. You cannot yet say “rerank is fixed — now also run summarize and answer on the new docs.” `--only` is the live path. Full resume still needs the old wrap’s function pointers.

**Done when:** `argus replay <id> rerank --app ...` (without `--only`) continues the tail from the new update, still without wrapping compile.

### 2. Node function is read off a LangGraph-internal field

Replay finds `app.nodes[name].bound.invoke`. That is LangGraph’s own handle, not a public “give me node X” API. It works on current LangGraph. A future rename could break `--app` replay until we swap the accessor.

**Done when:** we use a documented API, or we pin / test the accessor in CI against the LangGraph versions we claim.

### 3. Empty corpus still fails the gate

If retrieve already got `[]` (nothing in the library), the run still fails — we treat “search found zero” as a silent failure. Rerank is no longer piled on as the origin (it only inherited emptiness). Summarize may still look shallow.

**Done when:** product decides whether empty-corpus is a pass (per-node opt-out) or stays a fail. Do not soften “retriever returned nothing” globally — that hides the flagship case.

### 4. `pytest --argus` is still the old wrap

CI eat-own-cooking (`tests/test_argus_ci_gate.py`, #68) still patches `StateGraph.compile` and Pregel `invoke` / `stream` / `batch`.

**Done when:** that test passes with `patch_graph` forced to raise.

### 5. HTTP / tool cassettes

Ledger has tool callbacks. No urllib3 monkeypatch on this path. No fake `http=[]` column.

### 6. Other frameworks / skinny traces

Recorder is LangGraph-specific. Skinny traces (payloads stripped) must refuse, not “pass.” CrewAI etc. later.

### 7. Do not delete `patcher.py` / `ArgusWatcher` yet

Blocked on (1) and (4). First `master` PR keeps the wrap beside the recorder.

### 8. Subgraph node names are bare, so two subgraphs can collide

`get_graph(xray=True)` gives `child:retrieve`, but the callback stream reports
`langgraph_node` as `retrieve`. We key on the bare name because the trace is
what we have to match. Two different subgraphs that each contain a `retrieve`
therefore collapse onto one registry entry.

**Done when:** the trace carries qualified names, or we correlate by
`parent_run_id` instead of by name. Not urgent — the ambiguity is in the trace,
not in our mapping.

### 9. `test_1mb_dict_completes` hugs its own budget

Pre-existing, unrelated to the pivot, but it will flake your CI. It asserts
`inspect_tool_outputs` on a 1MB dict finishes in under 60s and actually takes
33–55s on a dev machine — and >60s under any parallel load. It was written to
catch a 1051s pathology, so the threshold has three orders of magnitude of
slack against its real purpose and almost none against noise.

**Done when:** the budget matches the pathology it guards (say 300s), or the
test measures work done rather than wall clock.

### 10. Type drift is invisible

A node declaring `items: list[str]` and returning `"a,b,c"` grades clean. A
trace carries no successor type hints, so the structural check degrades to
`unannotated_successors` by design. The consumer map declares *who reads what*,
not *what shape* — a declared type would be the natural extension.

**Done when:** product decides whether `consumers` grows a shape, or this stays
a known limit of trace-based grading.

### Cosmetic

Judge auto-on when a key/login exists (`semantic_judge=None`).

---

## What we changed (this branch vs `master` @ 0.11.0)

| Area | Change |
|---|---|
| Capture | `ArgusRecorder` — callbacks only; `output_update` is the node’s return. Topology via `get_graph(xray=True)`, so subgraph nodes are graded |
| Notebook | `ledger.py` — rows from the run file; skipped steps omitted; reducer kinds persisted so piled-up lists survive reload |
| Contextual | Blame at the **reader**. Progressive fill is clean. Drop / never-written / written-empty still origin-blame |
| Inspector | Empty result: inherited `[]`→`[]` is warning; producer `[]` and drop-from-full stay critical. `empty_output` gated on `has_successors`, not on successor annotations. Crash walk extracted as `crash_origins()`. Placeholder-as-whole-answer promoted to critical |
| Crash blame | `session._blame_crash_origins()` fails the node that omitted the field; the correlator no longer overrides `root_cause_chain` on a crashed run |
| Signatures | RF-006 — single-occurrence lorem ipsum |
| Findings | `missing_field` keeps the authored reason (names the reader) and sets `origin_node` |
| Check | Unchanged verdict: `argus check` / `evaluate_run` |
| Replay | All replay paths re-feed **ledger** input. `replay_live(..., app=)` for trace runs. CLI: `argus replay <id> <node> --only --app mod:factory` |
| Judge | Last; cannot override criticals. Default on if a key is configured |
| pytest plugin | Uninstall restores Pregel methods. Plugin itself still wraps |
| CI | Workflows run on `pivot/**` branches |

Key files: `src/argus/recorder.py`, `ledger.py`, `contextual.py`, `replay.py`, `inspector.py`, `cli/cmd_replay.py`.  
Demos: `demo/fat_trace/`, `demo/new_user_rag.py`.

---

## Commits (from `master`)

| Commit | What |
|---|---|
| `6bafc9d` | Pivot notes in `CLAUDE.md` |
| `6c61a9a` | Fat-trace recorder, demo |
| `af25d3e` | Ledger, contextual, judge-last tests |
| `34ad325` | pytest `--argus` uninstall restores Pregel methods |
| `8703887` | Reducers on recorder path; skip investigation essay |
| `00aefdb` | `argus replay` fails loudly on a trace run without an app |
| `d774cf5` | CI on `pivot/**` |
| `4541291` | Emptied field counts as dropped |
| `182bb0d` | Judge on when a key is configured |
| `f7595a6` | Contextual: blame at the reader, not step 0 |
| `66f87ba` | A reader that produces the field it reads is not a failure |
| `781a772` | Ledger: a node that never ran is not a step |
| `8b3fa2f` | Ledger-sourced replay |
| `1b4f20e` | Green the pipeline |
| *(this push)* | Silent-failure stress matrix (`tests/test_silent_failure_matrix.py`, 28 tests / 7 pipelines) and the six detection fixes it found: router `empty_output`, subgraph grading, crash origin, placeholder severity, RF-006, finding reason |

---

## For contributors

- Work on **`pivot/fat-traces`**. Do not open a wrap-deletion PR into `master`.
- Do not rebuild `inspector.py` / signatures as new “layers.” They are the rules. The ledger feeds them.
- Do not stash function pointers on the recorder to make replay work.
- **Touching detection? Run `tests/test_silent_failure_matrix.py` first.** It is
  the false-positive guard as much as the detection one — roughly half its tests
  assert a pipeline is *clean*. Making a rule fire harder usually reds one of
  those, which is the signal the rule went too far.
- Adding a rule that needs "what runs next": ask whether you need the successor's
  **type hints** (you will not get them from a trace — that is what `consumers`
  is for) or only that **something runs** (`has_successors`). Conflating the two
  is what hid gap 1.
- Full `pytest tests/` takes ~8 minutes on live embeddings. Use the file list
  above plus the matrix.

Untracked on purpose (not in the implementation): `docs/ARGUS-PIVOT*.pdf`, `docs/generate_pivot_*.py`, `demo/research_agent/`, `website/public/__artifact.html`.
