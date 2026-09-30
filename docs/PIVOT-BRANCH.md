# Pivot branch — contributor update

Branch: **`pivot/fat-traces`**  
Last updated: 3 Oct 2026 (the run reviewer; see **Where things stand** below).

Same product: silent failures, origin blame, CI gate (`argus check`).  
Different capture: fat traces → ledger (notebook) → rules, with an LLM reviewer that has to agree before a judgement call fails CI. No wrapping the graph engine.

**Do not merge wrap deletion into `master`.** The first PR into `master` is additive: `ArgusRecorder` sits next to `ArgusWatcher`.

---

## Where things stand (3 Oct 2026)

Read this first; the sections below are the detail, in the order things landed.

**New since 2 Oct: the run reviewer (#149).** A blind probe showed the rules failing
about half of the healthy runs they had never seen, and the per-step judge having no
effect on CI at all. Rules are now **strict** (fail CI alone) or **heuristic** (fail CI
only when an LLM reviewer independently verifies the same step), and a rule *warning*
the reviewer verifies now fails CI. Without node purposes nothing about the reviewer
runs; the deterministic fixes that came with it (D14, D2, the contextual guess) apply
either way. Later the same day: a finding no rule can see now fails CI when **two
different models** verify it, three vendor shapes were added, and `pytest --argus` moved
onto the recorder with nothing patched (#78). Across 140 labelled faults and 61 healthy
runs, rules + reviewer went from 92 to **125 faults caught (89%)**, with 4 healthy runs
failed both before and after (3 of them a KYC config gap). Start at
**[The run reviewer](#the-run-reviewer-two-checks-must-agree-149)**, then the two sections
after it.

**Open work, by GitHub issue** (nothing else is tracked as open):

| Issue | What | Blocks |
|---|---|---|
| ~~#78~~ | **Done (3 Oct):** `pytest --argus` records through a LangChain configure hook, nothing patched; see "`pytest --argus` on the recorder" at the end. Was assigned to @Sravan1011; close or reassign on GitHub | — |
| #152 | `pytest --argus` binds runs by diffing `.argus/runs`, so parallel tests can steal each other's run. A fix (#153) is on `origin/pivot/fat-traces`; pull before touching the plugin | — |
| #83 | Delete the wrap path (`patcher.py`, `watcher.py`, `http_recorder.py`). #78 no longer blocks it; replay continuing the tail (below) still does | — |
| #91 | Recorder lock is held across grading: fan-out serializes and LLM calls block every callback | — |
| #149 | **Built** as the run reviewer (last section). Purposes live in the `argus baseline --purposes` file or `purposes=`. Open: a live-model test in CI, and an eval nobody on the team wrote | — |
| #57 | One-file GitHub Action for the CI gate | — |
| #49, #25 | Re-triage signature severities; more `argus doctor` checks | — |

**Eval defects (`test-cases.md` §6):** E1, E2, E4, E4b, E5, E6, E7, E8, E9 are
fixed. E3 is fixed only for its narrow shape (a short policy decline that cites a
number and a reason stays a warning); the broader "ambiguous tier" idea in #130 is
not built.

**Suite size:** 1,335 tests pass (5 skipped, 2 xfailed); the two matrices below are 89 tests between
them (the per-section counts further down are the counts at the time and are
older). Run both matrices after any detection change.

**Not this milestone** (per `CLAUDE.md`): GitHub App / auto-PRs, production Slack,
cloud UI polish, new framework adapters.

---

## What we shipped on this branch

The loop a user can run today:

```text
ArgusRecorder.attach(app)     # listen; do not patch compile / invoke
  → fat trace (node, input, what the node returned, tools, errors)
  → ledger (notebook from the saved run file)
  → rules (contextual + inspector + signatures — not an LLM)
  → LLM judge last (on if a key exists; reviews soft flags only; cannot originate a fail)
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

Proof (run these first; the full suite is fine too):

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
actually catch what enterprises ship?". 39 tests over eight real LangGraph
pipelines — supervisor/worker loop (cyclic + conditional), map-reduce fan-out
with `operator.add`, a four-node CRM triage chain, a tool-calling fetcher, a
degraded-output generator, a crash handoff, and a subgraph. `patch_graph` is
monkeypatched to raise in every test, so a slide back to the wrap path fails
loudly.

Every test asserts **the blamed node**, not just "something was found". A test
that only checks `passed is False` would also pass on the old behaviour, which
blamed the crash site.

> **Read "What the matrix does NOT cover" below before treating this as
> validation of the whole architecture.** It covers the detection core, not
> streaming, not `create_react_agent`, not real model calls.

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

When several contextual findings blame the same origin, every authored reason
is appended to that step's inspection message. Any earlier structural or tool
message is preserved instead of being overwritten.

```bash
PYTHONPATH=src pytest tests/test_silent_failure_matrix.py -q   # 39 passed, ~20s
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

- **A node name shared across subgraphs is qualified (#95).** Row 2 above strips
  the `parent:` prefix. That merged two copies of one sub-agent into a single
  `retrieve`, which shared its edges and rows, so a write in one satisfied a read
  in the other. When a bare name occurs more than once (counting subgraph
  parents, so an inner `a` inside subgraph `a` counts too), those nodes are now
  recorded, blamed and edge-mapped as `a:retrieve` / `b:retrieve`. The name comes
  from `langgraph_checkpoint_ns` with task ids stripped, which equals the xray id.
  Names that do not collide stay bare. A consumer map for a colliding node must
  use the qualified name.

- **The consumer map checks every visit of a reader, not the first (#93).**
  In a loop, a field that is present on pass 1 and dropped before pass 2 fails on
  the node that dropped it. The reverse, a field that is not written yet on pass 1
  but is there by the reader's next visit, is progressive fill and stays clean. A
  field that is still missing on the reader's last visit fails as before.

- **Healthy loops no longer carry two warnings (#150).** `ordering_anomaly`
  (TC-002) ignored `retried` visits when it worked out which node ran first. A
  loop's first pass is exactly what gets relabelled `retried`, so `worker` looked
  like it ran before `supervisor`. Retried visits count now; only `skipped` ones
  (which never ran) are left out. Separately, `BA-005` no longer asks an
  *inferred* `structured_json` node to be nested (see the limitations table).
  `pivot_eval` / `ship_eval` pass counts are unchanged.

- **A run leaves no recorder bookkeeping behind, however it ends (#92).**
  `_finish` now calls `_forget(root)`, which drops every entry routed to that
  run. Before, entries were removed only by their own end callback. A tool whose
  end never arrived, or a step whose end never arrived (the trace `finish`
  refuses), stayed for the life of a served app, input-state snapshot included.
  Tools have their own `_tool_root` map, because putting them in `_root_of` would
  let a chain started inside a tool be recorded as a second step of its node.
  Normal, crashed, interrupted and cancelled runs already cleaned up.

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
- A trace that is complete but **skinny in shape** — each step's output is the
  merged state after the node, not the dict it returned — also refuses (#82,
  `grading._merged_state_steps`). On merged state `{}` is unreachable, so
  `empty_output` never fires and every run grades clean. A step "looks merged"
  when every input key survives into its output and at least one is carried
  unchanged (or, under an append reducer, only extended); `{"messages": [new]}`
  does not start with the input's messages, so `MessagesState` agents pass.
  Refuses at ≥ 2/3 of ≥ 3 steps with non-empty input — one pass-through node
  (`{**state, ...}`) is a real update. Measured: 0 refusals over 1,377 real
  traces (both matrices, pivot_eval, ship_eval; worst share 1/3), 425/429 of the
  same traces refused once rewritten as merged state. The misses are graphs
  where every node overwrites every key it received — indistinguishable by shape.

### The shipped-shapes matrix — and the nine defects it found

`tests/test_shipped_shapes_matrix.py` closes the three highest-value holes the
detection matrix left open: **`create_react_agent`**, **`MessagesState` /
`add_messages`**, and **`.batch()` / repeat `invoke`**. 29 tests, deterministic
(a scripted fake model), same contract as the sibling matrix — `patch_graph`
raises, every test names the blamed node, and half of them assert a pipeline is
**clean**.

Each of those three shapes was hiding a defect. Nine in total, all fixed:

| # | Where | What was wrong | Consequence |
|---|---|---|---|
| 1 | `anomaly_detector.py` | BA-004 matched refusal phrases in *any* string, including input a node passed through | A ticket reading "I cannot reset my password" made the classifier that normalised it a **critical** failure. Every desk / chat pipeline failed CI on the customer's own words |
| 2 | `recorder.py` | One session per `attach` | Attach once, invoke twice and run 2 appended to a finalized session: never saved, never graded, no error |
| 3 | `recorder.py` | `.batch()` items (parallel threads) folded into one run | The running state became a merge of two different inputs, so a field written by item B read as present for item A |
| 4 | `inspector.py` | `_RESULT_NAME_RE` matched `response_metadata` — `metadata` ends in `data` | Every LangChain message carries an empty one, so **every healthy react agent** failed on `empty_result` |
| 5 | `data/signatures.json` | MP-006 matched any single-line JSON ending `"…}` | Every structured tool return was a **critical** `malformed_payload` |
| 6 | `recorder.py` | Step dedup compared only against open `_pending` | A react agent filed **4 `agent` rows for 2 turns**; the phantoms pushed the real rows into `retried`, which the gate skips |
| 7 | `ledger.py` | `add_messages` folded as overwrite (its callable is `_add_messages`) | Every `MessagesState` graph's running state held only the last node's messages |
| 8 | `inspector.py` | Tool-failure rules never decoded JSON-encoded strings | LangChain stringifies tool returns, so a swallowed HTTP 500 in a react agent graded **clean** |
| 9 | `inspector.py` | `_is_the_whole_answer` could not walk `[0]` path segments | The whole-value placeholder promotion was dead for `MessagesState`, i.e. for every agent |

1, 4 and 5 are false positives that make the gate unusable; 3, 6, 8 and 9 are
false negatives that let real failures ship; 2 grades nothing at all. Running
the new file against the pre-fix tree failed 9 of its original 17 tests, so it is a real
regression guard and not a restatement of current behaviour.

```bash
PYTHONPATH=src pytest tests/test_shipped_shapes_matrix.py -q   # 29 passed, ~25s
```

Also verified during that work, and previously unverified: `.stream()` /
`.astream()` parity with `invoke`, subgraphs **two levels** deep, a 12-way
contended fan-out (no step lost or duplicated, one silent shard blamed alone),
custom-reducer fan-in blame, interrupt / checkpointer / resume (a paused run is
never graded clean; the resumed half is graded on its own), oversized-payload
truncation markers, and live OpenAI pipelines.

### The new-user walkthrough — driving the CLI, not the internals

Both matrices call the Python API and assert on `RunRecord`. That is not how a
user meets ARGUS. This pass built three pipelines of rising difficulty in a
clean workspace and drove them the way the README does — run the graph, then
`argus check` / `show` / `fix` / `diff`, then `pytest --argus` — with real
`gpt-4o-mini` on the model nodes and **the LLM judge left on by default**.

| Level | Pipeline | Sabotages |
|---|---|---|
| 1 | support-desk RAG: classify → retrieve → draft → send | retriever drops what it found |
| 2 | `create_react_agent` + two tools | backend 500s · lookup returns `[]` · model refuses |
| 3 | research desk: planner → **3-way parallel fan-out** → **subgraph**(rank → write) → reviewer on a **conditional loop** | branch returns `{}` · node two levels down returns `{}` · long-range contract dropped · branch echoes its input |

**True positives: 7 of 8, each blamed on the right node** — including a silent
`rank` two levels inside a subgraph, where the model then hallucinated an
unrelated job description and the pipeline's own LLM reviewer *approved* it.
The one miss is listed below.

The judge being on is what made this pass worth running. It found five defects
the judge-off suites structurally could not:

| # | Where | What was wrong |
|---|---|---|
| 10 | `session.py` | **The judge could fail a step no deterministic layer had flagged.** With `rules=[]` on every step, the same healthy `create_react_agent` failed **two runs in three** at confidence 1.0, with contradictory reasons. A gate that red-lights working pipelines at random gets switched off |
| 11 | `semantic_checker.py` | A tool-call turn (`content: ""`, payload in `tool_calls`) was judged as an empty answer. That is the normal shape of every tool-calling model |
| 12 | `semantic_checker.py` | The prompt's "empty field" rule was being applied to the **input**. On any message state the history contains empty-content tool turns, so the judge failed nodes for their input |
| 13 | `inspector.py` | `content: ""` next to a non-empty `tool_calls` raised `empty_result`. Warning-level — but the judge then read it as evidence and turned it critical |
| 14 | `inspector.py` | `json_in_string` fired on every `ToolMessage`, whose content LangChain always json-encodes |
| 15 | `inspector.py` | Every node of every clean run said **"pass (warnings) — add type hints to X"**. On this path the recorder *itself* supplies unannotated placeholder successors, so the advice was both wrong (the user's nodes were annotated) and impossible to act on |
| 16 | `cli/main.py` | `argus fix last` and `argus locate last` did not resolve the `last` alias, though `show` and `check` do — and `argus show` prints "argus show last" as a hint |

Defect 10 is the important one, and it is a **deliberate semantics change**:

> The judge is a reviewer, not a second cop. It is called only when the rules
> left a *soft* flag (a warning-level signature). It may drop that flag if it
> is wrong. It cannot originate a fail and cannot clear a hard fail. Walking
> every node looking for hallucinations is how a healthy pipeline went red on
> one run in a hundred. `tests/test_judge_last.py` pins the contract;
> `test_shipped_shapes_matrix.py` pins both halves (a healthy agent survives a
> judge that always votes fail; a real failure still fails).

Warning-level *behavioural* anomalies deliberately do not corroborate:
`BA-005 structural malformation` used to fire on any flat dict, which is what a
normal LangGraph node returns, so counting it would let the judge fail almost
anything (the flat-dict case is fixed, #150; the rule still stays out of corroboration).

Verified working end to end from the CLI: `argus check` exit codes (0 clean /
1 unclean), `show`, `list`, `diff` (correctly reports "retrieve: silent failure
→ pass FIXED"), `fix` (names the file and line — `01_rag.py:50`), `doctor`, and
`pytest --argus` (healthy test passes, silent-retriever test fails).

### Semantic coherence — the judge's actual job

"Is the node doing the right thing?" — cake ingredients in, a paragraph about
helicopter rotors out — is the one failure class no deterministic rule can see.
Nothing is missing, empty, malformed or erroring. `04_coherence.py` in the
new-user suite makes exactly that pipeline.

Defect 10's first fix (require corroboration for *every* judge verdict) killed
this outright: the judge said *"completely unrelated to the input, which is
about ingredients for a recipe"* at confidence 1.0, and the run graded **clean**.
Blanket corroboration threw away the judge's only unique competence.

That carve-out is **closed**. Standing-alone `unrelated` / `contradiction`
failed healthy nodes at random (a different node each run). The judge now
only reviews *soft* flags the rules already raised; it cannot fail a clean
step and cannot clear a hard fail.

| `failure_kind` | Gates with no rule agreeing? | Why |
|---|---|---|
| `unrelated` | no | Review of a soft flag only; never originates a fail |
| `contradiction` | no | Same |
| `empty_or_missing` | no | The rules already do this, and more reliably |
| `other` | no | Includes any malformed or unparsable reply |

The prompt still says **`unrelated` means subject matter, never answer quality**
(a fan-out branch contributing one relevant fact is not unrelated; a short
label — verdict, category, routing key, score — is a classification result).
That text stays because the judge still *reviews* flags; it is no longer how
a build fails.

A healthy graph with no soft flags does not call the judge, so the old
"1 in 6 false fail on the reviewer node" path is closed. For a fully
deterministic gate use `ArgusRecorder(semantic_judge=False)` — it still
catches empty updates, dropped contracts, tool failures, crashes and
degraded output. Guarded by `tests/test_judge_last.py` and
`test_shipped_shapes_matrix.py`.

### What is still NOT covered

Nothing below is known-broken — it is **unverified** or a **pinned decision**,
which is a different and more dangerous thing to leave undocumented.

| Not covered / pinned | Why it matters |
|---|---|
| **Real LLM nodes in CI** | Both matrices use deterministic stubs. Live-model runs were exercised by hand against OpenAI (healthy pipelines clean, sabotaged pipelines blamed on the sabotaged node, stable over three runs) but no live test runs in CI |
| **A terminal node returning `{}`** | `empty_output` is gated on having successors, and an edge to `END` is not one. Defensible — a terminal `send_email` node legitimately returns nothing — but it is also the last chance to notice the answer was never produced. Workaround: declare `consumers={"answer": ["finalize"]}` |
| **A silent early iteration of a loop** | `_apply_loop_retries` relabels every earlier iteration `retried` when the final one passes, and the gate skips `retried`. Right for a genuine retry, wrong for a data accumulator where round 1's empty contribution is never superseded. Parallel `Send` workers are no longer caught by this (E4). The sequential case (E4b, #131) keeps the verdict of an earlier iteration that wrote `operator.add` (kind `"add"`); `add_messages` is still relabelled, so a ReAct recovery stays clean |
| **Custom reducers, at fan-in** | Still round-trip as `"overwrite"` (`ledger.reducer_kinds`), and no single successor's recorded input can show what a merge did, so a custom fan-in reads as the last branch winning. The *sequential* case is fixed — see "The notebook believes the trace" below |
| **`add_messages` id de-duplication** | Folded as plain concatenation, so an updated message counts twice. Pinned by a test |
| **Token accounting** | `llm_tracker` reads usage off the node's output dict, so a node returning `{"category": "..."}` records none. Verified identical on the old wrap path — pre-existing, not a pivot regression. `on_llm_end` would fix it on this path |
| **A victim flagged alongside the origin** | When an upstream `{}` starves a downstream model node, both are flagged. `first_failure_step` is still the origin, so the verdict is right and the extra finding is noise: `degraded_input` covers present-and-bad fields, not absent ones |
| **A node that echoes its input** | The one true-positive miss: a researcher branch returned the question verbatim as its note and the run graded clean. Echo detection exists for main answer fields but not for a fan-in accumulator. Related signal worth adding: the reviewer loop hit its revision cap and shipped anyway, which is itself evidence |
| ~~**`BA-005 structural malformation`**~~ | **Closed (#150).** The nesting demand now applies only to a *declared* behaviour type; an inferred `structured_json` (the fallback for any flat dict) no longer flags a flat dict for being flat |
| **A healthy fan-out + review pipeline, judge on** | Closed: the judge is not called unless a warning-level signature is already on the step. `semantic_judge=False` remains the fully deterministic gate |
| **Frameworks other than LangGraph** | The recorder is LangGraph-specific. CrewAI etc. later |

---

## Still to fix (open issues)

These are known. They do **not** block pushing this branch. They **do** block deleting the old wrap and merging a “replacement” PR into `master`.

### 0. What `replay` means — decided (#79)

**A rerun's state comes from the ledger; its code comes from the caller, or from
references the run recorded about itself. There is no third source.**

| Run kind | `argus replay` | Code from |
|---|---|---|
| Trace (`ArgusRecorder`) | `--only --app module:factory` | the caller's compiled graph |
| Trace, no `--app` | exit 1, naming `--app` *and* `argus check <id>` | nothing is imported |
| Legacy wrap (`ArgusWatcher`) | unchanged, labelled `(legacy refs)` | `node_fn_refs`, captured at record time |

What went away is the path that *manufactured* the references it lacked: `_auto_locate`
scanned the project — with an LLM — to guess where each node's function lived, imported
it and saved the guess back into the run file. That is "re-import live functions as the
default" (brief §5), and it failed opaquely: a wrong guess re-runs some other function
and reports it as your node.

Two options were on the table and both were rejected. *"Replay means re-score"* makes it
an alias of `argus check <id>`, which already reloads the run file, rebuilds the ledger
and re-runs every check — and it leaves `--set` / `--patch` meaningless, since patching
state only means something if something then executes. *"Drop re-execution entirely"*
would delete `replay_live`, which is the one piece of this that already works the way the
pivot wants; the issue's version of it also deleted `derive_node_fn_refs`, which
`argus locate`, `argus fix` and the UI all still use. `source_locator` stays for them.

Tests: `tests/test_replay_semantics.py`, one per branch, including a guard that replay
never reaches for `source_locator` again.

### 1. Replay does not continue the graph after the fixed node

You can rerun `rerank` from the notebook. You cannot yet say “rerank is fixed — now also run summarize and answer on the new docs.” `--only` is the live path. Full resume still needs the old wrap’s function pointers.

**Done when:** `argus replay <id> rerank --app ...` (without `--only`) continues the tail from the new update, still without wrapping compile.

Until then that non-`--only` path runs through the legacy `ArgusWatcher` wrap (`_replay_with_factory`). Its factory may return the `StateGraph` or `graph.compile()`. The compiled form used to be refused ("must return a StateGraph or CompiledGraph. Got: CompiledStateGraph"): the unwrap looked for `.graph`, and LangGraph 0.2+ exposes the builder as `.builder`. Guarded by `test_an_app_factory_returning_a_compiled_graph_replays`.

### 2. Node function is read off a LangGraph-internal field

Replay finds `app.nodes[name].bound.invoke`. That is LangGraph’s own handle, not a public “give me node X” API. It works on current LangGraph. A future rename could break `--app` replay until we swap the accessor.

**Done when:** we use a documented API, or we pin / test the accessor in CI against the LangGraph versions we claim.

### 3. Empty corpus still fails the gate

If retrieve already got `[]` (nothing in the library), the run still fails — we treat “search found zero” as a silent failure. Rerank is no longer piled on as the origin (it only inherited emptiness). Summarize may still look shallow.

**Done when:** product decides whether empty-corpus is a pass (per-node opt-out) or stays a fail. Do not soften “retriever returned nothing” globally — that hides the flagship case.

### 4. ~~`pytest --argus` is still the old wrap (#78)~~ — done

`tests/test_argus_ci_gate.py` now forces `patch_graph` to raise and passes under
`--argus`. See "`pytest --argus` on the recorder (#78)" at the end.

### 5. HTTP / tool cassettes

Ledger has tool callbacks. No urllib3 monkeypatch on this path. No fake `http=[]` column.

### 6. Other frameworks / skinny traces

Recorder is LangGraph-specific. Skinny traces (payloads stripped) must refuse, not “pass.” CrewAI etc. later. Merged state posing as updates refuses too (#82) — re-measure its threshold when a new adapter lands.

### 7. Do not delete `patcher.py` / `ArgusWatcher` yet (#83)

Blocked on (1). (4) is done. First `master` PR keeps the wrap beside the recorder.

### 11. Recorder lock is held across grading (#91)

Fan-out serializes behind it, and a judge call blocks every other callback.
Correct but slow; matters for wide fan-out and for a served app.

### 12. Semantic failures are mostly invisible to the rules (#149)

Rules catch about 90% of *mechanical* silent failures and very little semantic
ones (see "How far the 90% travels"). Partly answered by the run reviewer (last
section): its verified findings fail CI where a rule agrees and are advisory
otherwise. A decision taken against the evidence with no rule signal on that
step (a date-format change that wrongly declines a customer) is still advisory
at best.

### 8. ~~Subgraph node names are bare, so two subgraphs can collide~~ — done (#95)

Colliding names are now qualified (`a:retrieve`) from `langgraph_checkpoint_ns`;
see "Behaviour changes" above. Remaining edge: a `Command(goto=...)` issued
*inside* a subgraph to a colliding name is not merged into the edge map.

### 9. ~~`test_1mb_dict_completes` hugs its own budget~~ — done

Threshold relaxed from 60s to 300s (`0fafe24c`); it still catches the 1051s
pathology it was written for.

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

## What we changed (this branch vs `master` @ 0.11.0) — first slice

Later changes are in their own sections; this table is the original pivot.

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

This list stops at the first matrix. Everything after it is in the sections
below, each tagged with its issue number; `git log pivot/fat-traces` is the
authoritative history.

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
- Full `pytest tests/` runs in ~15s now: embeddings are opt-in
  (`ARGUS_EMBEDDINGS=1`) and off by default, so nothing calls out mid-grade.
  Turning them on costs a synchronous OpenAI round trip per unique string
  value — the old default, and why grading was minutes and leaked node data.

- **Testing the branch?** `test-cases.md` at the repo root lists the pipeline
  shapes, the healthy traps, the faults to inject (rule-visible and semantic,
  G1–G9), the judge contract, and the open defects (E1–E9) with their issues.

Untracked on purpose (not in the implementation): `docs/ARGUS-PIVOT*.pdf`, `docs/generate_pivot_*.py`, `demo/research_agent/`, `website/public/__artifact.html`.

---

### Composition, subgraph scope, and blame messages — four more defects

Three issues off the pivot backlog (#87, #89, #100). Each looked like a small
fix and each was hiding a case where ARGUS **graded a broken pipeline clean** —
the one outcome the brief bans.

| # | Where | What was wrong | Consequence |
|---|---|---|---|
| 1 | `recorder.py` (#87) | `attach()` returned `app.with_config(callbacks=[self])`. LangGraph's own `ensure_config` **overwrites** the callbacks key instead of merging it, so a Pregel's bound callbacks are dropped the moment a caller passes its own | Compose the graph into anything — `prompt \| app`, a graph used as a tool, a LangServe route — and ARGUS recorded **nothing**. No session, no run file, no verdict, and no error either, because `_finish` was never reached. `argus check` had nothing to grade and said so by saying nothing |
| 2 | `recorder.py` (#87) | Attribution treated "parentless chain" as the run boundary | Once the callbacks *were* delivered, the graph's chain arrives as a child of the outer framework's chain. Unattributable, so every node span under it was dropped too |
| 3 | `recorder.py` (#89) | Nothing asked whether a subgraph contributed to the **parent** state | An inner node writing an inner-only key returns a perfectly non-empty update, so `empty_output` stays quiet, and the subgraph parent row is deliberately not recorded. The parent state gained nothing, the next node read it unchanged, run graded **clean** |
| 4 | `ledger.py` (#89) | The notebook folded inner-only keys into the running state | `contextual` reads that state to decide whether a declared consumer got what it reads. A node declared to read an inner-only field looked satisfied, the origin went unblamed, and the run graded clean while the reader actually got `None` — a missed detection in the layer whose whole job is catching it |

Plus #100 from an outside contributor: `_blame_origins` overwrote
`inspection.message`, so when one node missed several declared fields only the
last reason survived, and any structural or tool message already on that step
was clobbered.

**Fixes.** (1) `attach()` returns a `RunnableBinding`, which merges through
langchain's `merge_configs` — correct for the list-plus-manager case — and
still proxies `nodes` / `get_graph` / `stream` / `batch`, so callers keep the
graph API. (2) The enclosing chain of a `langgraph_node` callback *is* the
graph run, whatever ran above it, so adopt it; a node span with no enclosing
run at all refuses loudly rather than vanishing. (3) `_blame_barren_subgraphs`
asks the subgraph-level question — did any inner update touch a key the outer
graph has? — **not** per inner node, because an early node writing a scratch
key to feed a later one contributes nothing outward and is ordinary work.
(4) `RunRecord.state_keys` scopes the notebook to the graph's own keys,
persisted so a reloaded ledger folds like the live one; the row's `update`
still reports what the node returned.

**One trap worth knowing if you ever mark a step from outside the normal
path:** `retried` is assigned in *finalize*, after the per-step layers run, and
both `check.evaluate_run` and `collect_findings` drop retried steps. Blame
written onto the first visit of a repeated node is therefore invisible and the
run still goes out clean — which is how a subgraph on a loop edge that never
contributed kept passing even with fix 3 in place. Blame goes on the earliest
inner node's **last** visit.

Guards live in both matrices and were each verified to fail when the mechanism
is reverted — including the tempting wrong variants (per-node instead of
subgraph-level, first visit instead of last, scoping the reported update as
well as the notebook, and `with_config` instead of the binding).

```bash
PYTHONPATH=src pytest tests/test_silent_failure_matrix.py tests/test_shipped_shapes_matrix.py -q   # 68 passed
```

**Still open, deliberately:** nothing scopes a subgraph's running state *within*
the subgraph — inner steps read the outer notebook, so an inner node reading a
sibling's inner-only key is not modelled. It costs nothing today (the row's own
`input_state` is what the node really saw) and would need scoped running state
to do properly.

---

## File ingest — grade a LangSmith export with no app (S-4 … S-12)

The recorder needs your process. This path needs nothing but the trace: point
`argus` at a LangSmith JSONL export and get the same verdict. Same inspector,
same ledger, same `argus check`. `src/argus/ingest/langsmith.py` imports
nothing from `langgraph` or `langchain_core` — a test pins that.

```bash
argus ingest langsmith trace.jsonl \
  --edges edges.json \          # real topology; without it, guessed from step order
  --consumers consumers.json    # {"field": ["reader", ...]} — who reads what, later
argus check last                # exit 1 when the run was not clean
```

| Step | What it added | Why it matters |
|---|---|---|
| S-4 | Tool child runs land on the step's ledger row | A tool that 500s and gets swallowed is graded from a file, same as live |
| S-5 | `argus edges` exports topology; `--edges` consumes it | A trace holds no graph. Without this, successors are guessed from step order and a fan-out reads as a chain |
| S-6 | A skinny trace **refuses** instead of passing | `hide_outputs=True` makes every run look like `{}`. Grading that reports every node as a silent no-op. Refusing (exit 2, nothing saved) beats a confident wrong verdict |
| S-7 | `--consumers` wires the contextual layer | Blames the node that **dropped** a field, not the node where the gap surfaces. Without the map the drop is invisible — a test pins exactly that |
| S-8 | Model runs become `llm_usage`; `finish_reason` recorded | Token totals per run, and a `truncated_llm_output` **warning** when a call stopped at its limit. Warning, never critical: a cut-off answer may still be usable |
| S-10 | `langgraph>=0.6` floor; CI matrix over the floor and 1.x | The recorder path needs 0.6+; on 0.2.74, 56 pivot tests fail before it runs. Five CI legs (3.9 excluded from 1.x, which needs 3.10+) |
| S-11 | Crash fixture: a raised node keeps crash blame from a file | `lookup` writes `{"policy": {}}`, `price` reads `state["policy"]["number"]`. Blame stays on `lookup`, not the crash site and not the bystander in between |
| S-12 | A reloaded step keeps its `llm_usage` (BUG-2) | `_deserialize_event` never read it back, so every reloaded run showed 0 tokens. S-8 fixed the run totals; this fixed the per-step calls |

**One defect the merge itself found.** S-6's guard refuses a trace whose root
run has no `outputs`. A graph that **raised** has no final state to export, so
its root outputs are `{}` — and S-11's crash fixture was refused instead of
graded. Each PR was green alone; together they dropped the run ARGUS most wants
to grade. The guard now exempts a root carrying an `error` (the no-inputs check
still runs for it), pinned by
`test_a_crashed_root_is_not_read_as_a_hidden_outputs_export`. Worth
generalising: **a refusal rule written against one shape of missing data will
eventually refuse a real failure.** Ask what else produces the absence.

Fixtures are generated, not hand-written, and carry no host details:
`scripts/make_langsmith_fixture.py [--tool|--drop|--llm|--crash]` traces a real
graph under LangChain's own tracer with a stub client, strips `extra.runtime`,
and rewrites traceback paths to `<site-packages>` / `<repo>`. Regenerate rather
than edit the JSONL by hand.

**PRs on hold, on purpose:** #71 (UI redesign), #76 (batch coverage in the
pytest plugin) and #84 (ledger reducer `state_after`) target the **wrap** path
and are not being merged into this branch. They are not stale — they are the
old architecture. Do not rebase them onto `pivot/fat-traces` without a ticket
that says to.

---

## `Command` handoffs were losing their update (#88)

`Command(goto=..., update={...})` is the modern LangGraph handoff — what every
supervisor and multi-agent example emits. It is not a dict, and `_close_step`
read the update as `outputs if isinstance(outputs, dict) else None`, so the
whole update was discarded and the step filed as "unreadable shape".

That is three misses at once, and the run graded **clean** through all of them:

| | Before | After |
|---|---|---|
| Ledger | `update=None` — the notebook had no record of what the node wrote | The update is on the row |
| `empty_output` | Could not fire; `Command(goto=..., update={})` — a silent no-op — read as "no update we can read" | Fires, critical, on that node |
| Consumer map | `contextual._wrote()` was False for every field the node actually wrote, so blame went to whoever ran next | Blame lands on the writer |

**The distinction that carries the fix:** `update=None` (`Command(goto="next")`
— routing and nothing else) is *not* the same as `update={}`. The first claims
no update; the second claims an empty one. Collapsing them (`outputs.update or
{}`) is the tempting wrong fix — it fails every working supervisor, and there
is a test that fails on exactly that and nothing else.

A pair-sequence update (LangGraph also accepts `[("k", v), ...]`) is folded to a
dict rather than lost the same way. Anything that will not fold is returned
untouched and lands in the unreadable branch — never an exception, because **a
recorder that raises takes the user's graph down with it.**

Seven tests in `tests/test_shipped_shapes_matrix.py` (section 6), including one
on the annotated `-> Command[Literal["write"]]` form, which is the only one
running against a *true* edge map — without the annotation LangGraph cannot
draw the `supervise -> write` edge and reports `supervise -> __end__`. Each
mechanism was verified to fail when reverted: dropping the unwrap fails 6,
collapsing `None` into `{}` fails exactly the false-positive guard, and letting
an unfoldable update raise fails exactly the crash-resistance guard.

**Left out, deliberately:** the issue also suggests putting `goto` on the ledger
as a routing column. No rule consumes it today, and it needs a model + storage
round trip, so it is not in this change. Worth doing when something reads it —
the obvious candidate is checking a `Command` route against the declared edge
map, since a dynamic `goto` is invisible to `get_graph`.

---

## A dynamic `goto` was invisible, so `empty_output` stopped firing (#110)

Follow-up to #88, and the reason the `goto` column it suggested was worth
building after all.

The destination annotation is **optional** in LangGraph. Without it,
`get_graph` cannot see where a `Command` routes:

```python
def supervise(state) -> Command:                    # no annotation
    return Command(goto="write", update={})
# edges: [('__start__','supervise'), ('supervise','__end__')]   ← supervise -> write missing
```

`empty_output` is gated on `has_successors`, which comes from that map, so the
node looked terminal and the rule did not fire — on exactly the handoff shape
#88 was about. The annotated twin failed correctly. **Same graph, same
behaviour at runtime, different verdict**, decided by a typing choice.

**Fix.** `_command_goto` reads the route off the `Command`, and
`_observe_route` merges it into the edge map *before* the step is graded —
`empty_output` reads the map inside `on_node_end`, so after would be too late.
Two things it deliberately does not do:

- **Invent nodes.** Only destinations the graph declares are added. `__end__` is
  not one, so `goto=END` adds no successor and a terminal node stays exempt.
  That is the single filter — an earlier version also stripped sentinels inside
  `_command_goto`, which no test could fail independently because the
  known-nodes filter already covered it. One guard, in the place that has the
  node list.
- **Filter on `names` alone.** `_topology` returns subgraph *parents* separately
  (their rows are not recorded), so a filter built from `names` silently drops
  `supervise -> docs` and hands #110 straight back for any graph handing off
  into a subgraph. The known set is `names | subgraphs`.

An observed route is one branch, not all of them — an untaken branch stays
unknown. That is the same bargain `ingest/langsmith._step_order_edges` already
makes when a trace carries no graph, and "reaches something" beats "terminal".

**Behaviour change worth knowing.** Un-annotated supervisors that were passing
will now fail if they hand off with `Command(goto=..., update={})`, because
that claims an *empty* update and a router is still a node (the gap-1 rule
above). This is not new policy — the **annotated** form has always failed that
way; #110 only makes the un-annotated form agree. Write `Command(goto=...)`
with no `update` to claim no update. `test_the_destination_annotation_does_not_
change_the_verdict` runs the same supervisor loop both ways and asserts the
verdicts are identical.

**The same hole, other routing form (#151).** `add_conditional_edges("worker",
route)` with **no path map** is just as invisible: `get_graph` draws
`worker -> __end__`, so a `worker` returning `{}` looked terminal and the run
blamed its victim downstream. LangGraph runs the path function as a child
runnable inside the source node's task, and that child's output *is* the route
(`"supervisor"`, a `Send`, or a list). It ends before the node's step closes,
so `_end` collects it for sources listed by `_unmapped_branch_sources` (top-level
and subgraph builders), and `_close_step` passes it to `_observe_route` together
with any `goto`. Same filter, same bargain: only declared nodes, only the branch
taken. Guarded by `test_a_worker_no_op_on_an_unmapped_conditional_edge_is_blamed`
and `test_a_healthy_unmapped_loop_is_clean` in the silent-failure matrix.
Limitation: a subgraph's path function returning a node name that is *qualified*
(#95) is not matched.

Eleven tests in `tests/test_shipped_shapes_matrix.py` section 6 now (#88 + #110).
Each mechanism verified to fail when reverted: dropping the observed route fails
4, dropping the known-nodes filter fails exactly the `goto=END` terminal guard,
filtering on `names` alone fails exactly the subgraph-handoff test.

**The route is now a ledger column.** `_observe_route` consumes the `goto` to
repair the edge map, but the notebook had no place for it — so the one thing
that explains *why* the next node ran was the one thing the trace did not keep.
`NodeEvent.goto` / `LedgerRow.goto` hold the route actually taken (empty for a
node that returned a plain update), and it survives the save/reload round trip.
This is the column #88 suggested and deliberately deferred for having no
consumer; #110 is the consumer.

---

## A `Command` handoff graded clean on the ingest path (#111)

The recorder-path sibling of #88/#110, and the worst of the three: this one
went out **clean**, with no overlapping anomaly carrying it.

LangSmith exports a `Command` return as its **repr**, not as data:

```json
{"name": "supervise", "outputs": {"output": "Command(goto='write')"}}
```

Read as the node's update, that is a field the graph never wrote. It looks
non-empty, so `empty_output` cannot fire; `contextual._wrote()` goes true for a
key that is not in the state schema and false for everything the node really
wrote, so a declared consumer blames the wrong node; and `argus show`, the
ledger and the UI all display a field the pipeline never had.

**Why it cannot simply be parsed back.** `Command`'s repr **omits `update` when
it is falsy**:

```
Command(goto='w', update={})  ->  "Command(goto='w')"   ← the silent no-op
Command(goto='w')             ->  "Command(goto='w')"   ← legitimate routing
```

Byte-identical. The one distinction #88 and #110 established as load-bearing is
destroyed by the serialisation, and reconstructing the update from the merged
state is the skinny-trace reading the pivot rejects. ARGUS genuinely cannot
know whether that node was fine.

**So it says so.** `_command_repr` recognises the shape — a lone `output` key
whose string value starts with `Command(`, which keeps a node whose state
really has an `output` field out of it — and the step is recorded with **no**
update plus a critical `unreadable_update` signal. Critical, not a warning, on
the rule that makes the whole product work: *"I could not read this node's
update" and "this node ran fine" must not be the same verdict.* That is S-6's
line (`an incomplete recording is not a pass`) scoped to one step instead of a
whole trace. Only the unreadable step is affected; the rest of the trace grades
normally.

**Consequence to expect:** any trace containing a `Command` handoff now fails
`argus check` until LangSmith exports the update structurally. That is the
intended reading — those runs were previously passing without being graded.

Wired through with it:

- `docs/STATUS.md` claimed `semantic_fail` was "only possible when
  `semantic_judge=True` and a provider key is configured". That has been wrong
  since critical anomaly signals started setting it, and `argus ingest` never
  runs the judge at all — so a user ingesting a Command trace saw a status the
  vocabulary said required a judge they had not enabled. Corrected, along with
  the warning/critical rules and the failure-type list.
- `website/lib/types.ts` `NodeEvent` gained `goto` (from #110) and `tool_calls`
  (missing since #86 — the interface documents itself as a mirror). `Finding.type`
  is a free string there, so the new type needs no UI enumeration change.
  Typecheck is unchanged at 8 pre-existing errors, all from an uninstalled
  `@supabase/supabase-js`.

Fixture: `scripts/make_langsmith_fixture.py --command`. Break proofs: not
detecting the repr fails 2, downgrading the signal to a warning fails exactly
the gate test, still recording the phantom field fails exactly the ledger test.

---

## The judge was blind across steps — it now gets the ledger (#85)

`check_semantic_coherence(node_name, input_state, output_dict)` judged one node
at a time, so it was structurally blind to the failure the contextual layer
exists for: a field written early, legitimately emptied partway through, still
needed much later.

```
search  -> {"docs": ["doc-1", "doc-2"]}
clean   -> {"docs": []}                 a filter that matched nothing — locally fine
summarize -> "summary of 0 docs"        honest about nothing — locally fine
```

Node-by-node, nothing here is wrong. The failure only exists in one field's
history, which the judge could not see.

**What changed.** The judge now also receives `prior_rows` — the `LedgerRow`s
for the steps before the one being judged, the same shape `build_ledger`
already produces — plus the declared `consumers` map. `ArgusSession.consumers`
carries the map that previously only reached `grading.finish`; `new_session`
sets it, the recorder and the LangSmith ingest both pass it.

**Scoped, not "pass the LLM everything".** `_tracked_fields` keeps the history
to the node's own I/O keys plus the fields `consumers` says it reads, and
`_history_lines` emits one line per prior step that *wrote* one of them. The
prompt grows with a node's contract, not with the run, and evidence about
fields the node never touches — the thing that makes a judge invent failures —
never reaches it.

**Still last, still not the verdict.** Opt-in (`semantic_judge`, auto-on only
when a key exists), still after the rules, still unable to override a critical,
and the new prompt clause tells it to report an upstream emptying as
`empty_or_missing` — a kind that already requires a corroborating rule finding,
so the added sight cannot gate CI on its own. `JUDGE_STANDALONE_FAILURE_KINDS`
is untouched.

Break proofs in `tests/test_judge_last.py`: not passing the rows fails
`test_the_judge_is_shown_the_step_that_emptied_the_field`; letting the judge
clear the dropper fails `test_history_does_not_let_a_judge_pass_clear_the_dropper`;
widening the scope to every field fails
`test_history_is_scoped_to_the_fields_the_node_touches`.

---

## The notebook believes the trace, not its own arithmetic (#80)

**What a contributor needs to know:** `LedgerRow.state_after` is a *reconstruction*,
and reconstructions can disagree with what really happened. When they do, the
recording wins.

### Why the fold can be wrong at all

LangGraph merges a node's update into state using the reducer declared on the
field — `Annotated[list, operator.add]` and friends. `build_ledger` has to redo
that merge to know what the state looked like after each step, and it cannot: a
reducer is a **callable**, and callables do not survive `.argus/runs/<id>.json`.
So the run file stores a *kind* per field (`reducer_kinds`, a string), and the
fold handles the kinds it recognises:

```
add        ->  running[k] = running[k] + value      # operator.add, add_messages
overwrite  ->  running[k] = value                   # everything else
```

Everything it does not recognise is `"overwrite"`. For a custom reducer — a
last-good-wins keeper, a dict merge, a max — that is a guess, and it was wrong
in the one direction that matters:

```python
def keep_best(old, new): return new if new else old   # docs: Annotated[list, keep_best]

search  -> {"docs": ["seed"]}
clean   -> {"docs": []}       # the real reducer keeps ["seed"]
```

```
REAL final docs                    : ['seed']
`summarize` input_state, as recorded: ['seed']     <- the run file's own answer
LEDGER state_after['clean']        : []            <- the fold's guess
```

The notebook contradicted the very file it was built from, and reported `clean`
as having emptied a field that was never empty.

### The repair

`_believe_the_trace` compares the fold against the **next step's recorded
`input_state`** — which is the merged state LangGraph really handed that step,
reducers already applied — and prefers the recording. Two properties make this
safe to rely on:

- **One-directional.** A correction only fires where the fold says empty and the
  recording says otherwise. It can restore a value; it can never invent an
  emptiness, so it cannot hide a genuine drop. `test_the_repair_never_invents_a_value_the_trace_does_not_show`
  pins both halves.
- **Built from recorded data only.** Nothing is imported, nothing is re-executed,
  so a live notebook and one rebuilt from the run file stay identical — the spike-2
  property, still guarded by `test_ledger_from_live_steps_matches_the_reloaded_one`.

The row's `update` is untouched: it still reports exactly what the node returned
(`{"docs": []}`). Only the reconstructed running state is repaired.

### What was rejected

Persisting each reducer's import path and re-resolving it on load. That makes
grading import the user's code — precisely what #79 decided replay must never
do. A grader that imports what it grades is a grader that can crash on, or be
changed by, the thing under test.

### Ceiling

Under fan-out the next step to run may be a sibling that never saw this step's
update, so a branch that really did empty a field can read as still holding the
pre-fan-out value, and a custom fan-in merge is still invisible. Blame anchors on
the reader's own recorded `input_state` (`contextual.py`), which this never edits,
so the cost is a row's display rather than a verdict.

---

## Parallel workers were graded as retries (E4)

Found by the real-team pipeline suite (claims, text-to-SQL, SDR, ReAct support,
KYC — `test-cases.md`). `_apply_loop_retries` grouped a node's runs **by name
only**, so two `Send` workers that ran side by side looked like a loop: the last
one passed, the first was filed `retried`, and the gate skips `retried`.

| Pipeline | Swallowed failure in the *first* worker | Graded |
|---|---|---|
| KYC | sanctions provider 503 on the primary name, written as "no hits" | **clean** — customer screened on the alias only |
| Claims | pricing API timeout on one line item, written as `$0` | **clean** — claim underpaid |

The same failure in the *last* worker was caught, so the verdict depended on
which item happened to be priced first.

**Fix.** The recorder keeps LangGraph's `langgraph_step`, qualified by the
parent checkpoint namespace (the step counter restarts inside every subgraph
invocation), as `NodeEvent.superstep`. `_apply_loop_retries` now treats the
node's whole final **round** as final: siblings in one superstep are graded
individually and never relabel each other. Earlier rounds are relabelled only if
every sibling in the final round passed. `superstep` is `None` for trace-file
ingest and the wrap path, which keep the old per-event behaviour.

**Unchanged on purpose.** A sequential loop still self-corrects: a ReAct agent
whose first `lookup_order` 404s and whose second succeeds grades clean with the
404 filed `retried`. `tests/test_fanout_siblings.py` pins both sides (first /
middle / last worker failing, healthy fan-out, the ReAct recovery).

**Visible side effect.** A worker whose legitimate result is an empty retrieval
list (`hits: []` on a clean sanctions screen) used to be hidden the same way;
it is now graded, and E2 (#129: empty `hits` / `results` / `docs` is always
critical) fires on it. That is E2's defect surfacing, not a new one.

The other defects the suite found (E1–E3, E4b, E5–E9) are #128–#136, listed in
`test-cases.md` §6 with the order to fix them.

---

## A node's own verdict is not a tool response (E1 / E9)

The error-key, success-boolean and status-word rules in `inspector.py` were
written for tool payloads and ran on every node's own update too. The same
shapes mean the opposite thing there:

| Shape | On a tool payload | On a node's own update |
|---|---|---|
| `{"status": "denied"}` | the call did not go through | the node's answer — a claim correctly denied |
| `{"ok": False, "errors": [...]}` | a broken response | a linter reporting what it found |

So every claims / lending / KYC / moderation pipeline failed CI on its normal
"no" path (E1), and every linter / guardrail / critic node was blamed beside the
node that actually produced the bad input (E9).

**Fix.** `inspect_tool_outputs(..., own_output=True)` — passed by
`inspect_transition`, not by `inspect_tool_calls` — makes two shapes *warnings*
on a node's own update: a status word, and a verdict about something else
(`errors: [...]`, plus an `ok: False` / `failed: True` sitting beside such a
list). Warnings are visible in `argus show`, do not gate, and are the soft flag
the ambiguous tier (#130) will review. The tool's own response is still graded
critically by `inspect_tool_calls`, so nothing that crossed a real boundary is
lost. The parameter defaults off, so a direct caller keeps today's reading.

**The line is narrow on purpose.** `errors: [...]` (plural, a list of findings)
is a report; `error: "API timeout"` (singular, truthy) is the node saying *it*
broke — the swallowed failure ARGUS exists for, and still critical. A
`success: False` with no findings list beside it is a stored tool result, not a
verdict, and stays critical too. The first cut of this fix softened every
verdict-shaped rule and reded seven tracked tests, each one a node reporting its
own breakage; those shapes are now pinned in
`tests/test_own_verdict_vs_tool_response.py` alongside the two defects.

Numeric HTTP status is untouched in both directions: no business decision is
"500".

---

## Tools the callback path cannot see — `report_tool_call` (F-29, second half)

Re-parenting inner-chain tool calls (F-29, first half) fixed the tools the
callbacks *reported* under the wrong run id. It could not fix the tools they
never reported at all.

Under langgraph 1.x, code running inside a node function can hold an empty
`CallbackManager`. A tool the node invokes directly —
`tool.invoke(args, config=config)`, the shape most hand-written nodes use —
therefore never fires `on_tool_start`, and `StepRecord.tool_calls` stays empty.
Everything downstream of that list goes quiet with it: `inspect_tool_calls` has
no payload to grade, so a 404 body, an empty result set, and a tool that raised
are all invisible. The node returns a plausible update and the run grades clean.
That is the exact outcome the brief bans, arriving through a hole in capture
rather than a hole in detection.

**The seam.** `report_tool_call(name, input=, output=, error=)`, exported from
`argus`:

```python
from argus import report_tool_call

def fetch(state):
    try:
        hits = search_tool.invoke(state["query"])
    except Exception as exc:
        report_tool_call("search", input=state["query"], error=exc)
        raise
    report_tool_call("search", input=state["query"], output=hits)
    return {"hits": hits}
```

It writes the record in the shape `on_tool_start` / `on_tool_end` produce —
`input` stringified the same way, `error` repr'd if it is not already a string —
so `_close_step` attaches it and the graders cannot tell a filed call from a
callback-filed one.

**Resolution order.** Recorder: explicit `recorder=` wins, else the module-level
current-recorder registry (set when a run attaches, cleared at run end *even on
a crash*). Step: explicit `config=` wins, else the ambient langchain config,
read by lazy import so argus does not hard-require langchain; from there, the
config's `run_id` walked up `_parent_of` to the nearest open step — or, inside a
node function where the config carries no `run_id`, the open step whose
`langgraph_node` matches, most recently started first.

**It never raises into user code.** A monitoring seam that takes the graph down
is worse than the gap it closes. Every failure path returns `False` after one
warning on the `argus` logger naming the reason: no active recorder, a
`recorder=` that is not one, or no open step to attach to.

The LangSmith ingest path is untouched — a trace file already carries its tool
child runs.

Pinned in `tests/test_report_tool_call.py`.

---

## Four defects from the eval run, merged from #139 – #142

`test-cases.md` §6 listed E1–E9 with the order to fix them. E1 and E9 went in
with `caa5f5a`, E4 with `cc39e0b`. These four close the rest of the batch that
had a ticket.

### A blank final reply graded clean (E7, #134 → #139)

`create_react_agent`'s last turn is an AI message with `content` and no
`tool_calls`. When the model returns empty content there, the customer gets a
blank reply — and the run passed.

The exemption written for #121 was the cause. A tool-calling turn legitimately
has `content: ""` beside a non-empty `tool_calls`; without skipping it, every
working agent raised a warning the judge then read as evidence, and a healthy
`create_react_agent` could not pass the gate. But the skip was keyed on the
*shape* being a message, so the final turn fell through to Rule 3 — which only
warns, because `content` is not a retrieval list like `hits` or `docs`.

Now an `ai`-typed message with empty `content` **and** no tool calls is critical
`empty_result`. The intermediate-turn exemption is untouched: it still checks
`tool_calls` first, so the two cases never meet.

### An own-router `KeyError` blamed a bystander (E8, #135 → #140)

`decide` ends with a conditional edge whose function reads
`state["risk_tier"]`. The field was never written, so LangGraph raises
`KeyError` — from inside `graph/_branch.py`, while evaluating `decide`'s own
routing.

The crash walk took that at face value: a node crashed on a field, so find who
should have written it and blame them. It landed on `aggregate_risk`, the last
node to touch the risk state — a node that did its job. The real bug is in
`decide`: its router reads a field its own update does not produce.

`crash_origins` now looks for the `graph/_branch.py` frame in the traceback.
When it is there *and* the crashed node's own update did not contain the field
either, the origin is the crash site. `_blame_crash_origins` then has to handle
a case it never saw before — origin and crash site being the same step, which
already carries `crashed` and usually no inspection at all. It builds one so the
missing field is still named in `argus check`, and deliberately does **not**
overwrite `crashed` with `fail`: the node did crash, and relabelling it would
lose that.

A `KeyError` raised in a node *body* has no branch frame and is unchanged — D1
and the silent-failure matrix still blame the upstream omitter.

### A nested field could not be declared (E6, #133 → #141)

`consumers={"email": ["send_email"]}` cannot catch a `compose` node that writes
`{"email": {"subject": "Re: your order", "body": ""}}`. The `email` key is
present and the dict is non-empty, so the contract holds while the customer gets
an empty email.

Consumer keys are now dotted paths — `{"email.body": ["send_email"]}` — resolved
against nested ledger state with the same never-written / dropped / written-empty
rules as a top-level field. `_resolve` walks the path and returns a `_MISSING`
sentinel when a segment is absent, kept distinct from a present `None` because
`allow_empty` treats those two differently.

Top-level behaviour is deliberately unchanged: a non-empty parent dict is still
not empty. Declaring `email` and expecting `email.body` to be checked would make
every partially-filled dict in every pipeline a failure. The leaf has to be
named.

### `allow_empty` stopped at the state field (E2, #129 → #142)

Fixing E4 had a side effect it predicted: a parallel worker with a legitimately
empty `hits` list used to be hidden by the sibling-relabelling bug, and once
siblings were graded individually it surfaced as a failure.

It is a real shape, not a regression. A KYC screen runs a name against OFAC and
a clean customer comes back `{"hits": []}` — the good path. `allow_empty` exists
for exactly this, but it only ever softened the node's **own update**. The empty
list here arrives in the *tool response*, which `inspect_tool_calls` grades
critically and separately, so declaring `allow_empty` on the state field never
reached it. Every healthy onboarding failed CI.

`node_writes_allow_empty(consumers, update)` answers one question — did this
node write a field declared `allow_empty`? — and `session.py` threads the answer
into both `inspect_transition` and `inspect_tool_calls`. Scoped to the writer on
purpose: the declaration says *this* field may legitimately be empty, so it
softens empty retrieval only where that field is being produced, not everywhere
in the run.

What still fails hard: a tool that raised, a 4xx/5xx body, an error key. And an
**undeclared** empty retrieval list stays critical — that is the RAG default, and
the whole point of a declaration is that it is a deliberate statement about one
field.

### Merge note

#139 and #142 both edit the payload scan in `inspector.py` and conflicted.
Resolved keeping both: #139's E7 branch sits above the
`_apply_tool_shape_rules` dispatch, which now takes #142's `allow_empty`
argument. Adjacent, not competing. Full suite green afterwards (1191 passed),
both matrices included.

## Enterprise-pipeline eval defects (S1–S10)

A local suite of six enterprise-shaped pipelines (prior auth, SRE incident,
recruiting, multi-agent travel, contract review, async month-end close) found
these. Every fix below was checked against a verdict snapshot of every local
and tracked suite: the only verdicts allowed to move are the targeted ones.

### Blame on a router-only node was dropped (S4)

A supervisor that routes with `Command(goto=...)` returns no update, so its
step is never inspected. When the contextual layer blamed it — a supervisor
that routed straight past `payment_agent` to the customer-facing summary —
`grading._blame_origins` found `inspection is None`, skipped the finding, and
the run graded **clean**. The finding itself was right; it was thrown away.

`_blame_origins` now builds the inspection the way `_blame_crash_origins`
already does for a crash site. A crashed step it had to build one for keeps
`crashed`; every other path is unchanged.

### `voided` was not a failure word (S6)

A DocuSign envelope voided because the signer's email bounced came back as
`{"status": "voided"}`; the node recorded it as sent and the run graded clean.
`voided` joins the status-word vocabulary. Same scope as every other word: a
tool response is critical, a node's own `{"status": "voided"}` is a warning
(E1/E9) — voiding a contract can be the node's decision.

### An empty FHIR `entry` or metrics `series` graded clean (S1 / S2)

`{"resourceType": "Bundle", "total": 0, "entry": []}` is a FHIR search that
found no patient; the prior-auth agent routed on and CI passed on a warning.
`{"status": "ok", "series": []}` from Datadog is a query that matched nothing;
it was not flagged at all, because `series` was not a result noun.

`entry` and `series` join `_RETRIEVAL_LIST_KEYS` (and `series` the result
nouns), so they grade exactly like `documents: []` — critical, softened to a
warning when the node writes an `allow_empty` field. Exact key match only:
`journal_entry: []` is untouched.

Side effect, accepted: a node that falls back to `{"series": []}` after a
failed call is now caught on its own update even when the call itself was
invisible (a plain HTTP client with no `report_tool_call`).

### A barren subgraph also pinned a bystander (S5)

A clause-extraction subgraph wrote only its scratch key. `subgraph_no_contribution`
caught it, and then the contextual layer added a second finding: "no step wrote
`clauses`" → the first row, `ingest`. When that was deferred, the next reader
(`playbook`, starved of `clauses`) was blamed for writing its own list empty.

`contextual_findings` takes `blamed_elsewhere` — nodes another layer already
failed for producing nothing (grading passes the barren-subgraph steps). With
one upstream of the reader, "never written" defers, the way it already defers
to a `{}` row, and the starved reader is recorded as a victim. The `{}` path is
unchanged.

**Blame moved to the exit node (decision reversed on record).** The finding
used to land on the subgraph's *first* inner node, justified only as matching
where the all-empty case blames. But what a subgraph hands back is whatever its
exit node writes, and an early node writing only scratch is the normal shape
(`test_a_scratch_key_feeding_a_later_inner_node_stays_clean`). So the finding
now lands on the **last inner step that ran**, which is also a final visit, so
loop-edge subgraphs stay visible. The matrix assertion moved from `normalize` to
`retrieve`. Snapshot diff: only `subgraph_no_contribution` blame moved (matrix,
contract review → `classify_clauses`, code-review bot → `collect`, due
diligence → `select`); no pass/fail changed.

### Async nodes on Python < 3.11 hide their tool calls (S8)

`async def post_journal(state)` awaiting `netsuite.ainvoke(args)` without
`config`: before 3.11, asyncio cannot pass langchain's callback context into
the child task, so the tool never fires a callback and a swallowed NetSuite 500
grades clean. Identical graphs grade correctly on 3.12, and on 3.9 when the node
forwards `config`.

Not fixable from the outside without patching the graph. `attach` now logs one
warning on the `argus` logger naming the async nodes, when Python < 3.11. No
verdict changes. Remedies: forward `config`, call `argus.report_tool_call`, or
run 3.11+.

### Not fixed in code, on purpose

- **S7** (a 403 on `ingest` also blamed `classify_clauses` through an undeclared
  subgraph scratch key): declaring the intermediate field —
  `{"sections": ["classify_clauses"]}`, which `argus consumers` suggests —
  makes blame exact. Widening the victim rule to undeclared fields would also
  hide real second failures in every pipeline.
- **S3, the rest** (`[PATIENT NAME]` in a letter, "Unable to determine root
  cause." as a diagnosis): still warnings. See the next section.
- **S9 / S10** (`argus consumers` misses graph-input fields and readers on an
  untaken branch): S9 is pinned by `test_a_field_nobody_wrote_is_omitted`, and
  S10 is inherent to reading one run. README now says both.

### A template instruction in finished output now fails CI (S3)

`"Limit Vendor liability to [INSERT CAP AMOUNT]."` went into a contract redline
and CI stayed green: PH-015 matched it, but PH-015 is a warning because most of
what it matches (`[TOPIC]`, `[Your Name]`, `{var}`) can be legitimate prose
(E5, #132).

**Rejected: let a judge-confirmed soft flag fail CI.** That reopens the path
this branch closed on purpose (`_corroborating_signal` / "only the rules fail a
build"). It would make the gate depend on which model the user has, whether a
key is set at all, and on a non-deterministic call. In the eval, gpt-4o-mini
confirmed 14 of 30 noise flags.

**Done instead, deterministic and narrow.** A PH-015 match is promoted to
critical only when all of these hold:
- it is an instruction slot, upper-case `[INSERT …]`, `[ENTER …]`, `[YOUR …]` or
  `[ADD …]`, which never belongs in finished output;
- it is in the node's **own** output (a tool may legitimately return a template);
- it is not in a field named like a template or prompt;
- the node **authored** it: a slot already in its input was forwarded, and
  blaming the forwarder added a bystander (`compliance` in the support desk)
  until this was checked.

Still warnings: `[TOPIC]`, `[Your Name]`, `{var}`, and all-caps labels like
`[PATIENT NAME]`, which collide with `[EXTERNAL EMAIL]` / `[URGENT REQUEST]`
banners in real mail. A non-answer written as prose ("Unable to determine root
cause.") has no deterministic shape. Those two belong to the parallel monitor
as advisory findings, not to the gate. Snapshot diff: the redline case is the
only verdict that moved.

## Whole-trace rules (`argus.trace_rules`) — coverage 51% → 90%

A taxonomy suite (23 failure classes injected at every node they apply to, 6
pipelines, 20 real vendor response shapes, 255 faults) measured ARGUS at 51%.
Fifteen rules, prototyped over the recorded trace and then built in, bring it to
**90.2%** with no false positive on 63 healthy runs, 54 of them real
gpt-4o-mini prose, and no verdict change on any healthy run in any existing
suite.

They run once, in `grading.finish`, before the contextual layer, because their
evidence spans steps or needs something one step does not carry. Each hit is a
critical `ToolFailure` on the step that caused it — the same mark
`subgraph_no_contribution` uses, so the roll-up, `argus check` and findings need
no new plumbing.

| rule | failure type | catches |
|---|---|---|
| D1 | `unknown_state_key` | a typo'd key LangGraph silently drops; blamed on the writer. Only when it is 1–2 edits from a *declared* key (consumer map or baseline) that is never written — extra keys (`reasoning` in a parsed reply, `result_a` beside `result_b`) are harmless and stay quiet |
| D2 | `error_response` | vendor error bodies the inspector missed (Salesforce `errorCode` lists, SOAP `Fault`, AWS `__type`, `errorMessages`, `message`+code ≥ 400, HTML error pages) |
| D3 | `empty_result` | an empty lookup whatever its keys (`totalSize: 0`, `Items: []`, `totalRows: "0"`, "No results found."). `None` / `""` are left alone: side-effect tools return them. Respects `allow_empty` |
| D4 | `unfollowed_pagination` | `has_more` / `next_page_token` and the node passed page one on as the whole list. Taking the top item is fine |
| D5 / D6 / D16 | `type_drift` / `sentinel_value` / `missing_output_key` | regressions against a healthy baseline (`argus baseline`): a type change, `N/A` / `unknown` / `-1` where data was, a key the node always writes gone (closes the terminal-`{}` gap) |
| D8 / D9 / D10 / D11 | `unrendered_template` / `degenerate_repetition` / `truncated_output` / `unparseable_model_json` | model output only (nodes with a recorded model call): `{{var}}`, lorem ipsum, `Dear [Name]`; repetition; `finish_reason=length` and the cut text used; JSON that did not parse |
| D12 / D13 / D14 / D15 | `ungrounded_number` / `near_miss_identifier` / `unperformed_action` / `stuck_loop` | a number nothing given supports (sums, differences, % changes allowed); an ID 1–2 edits off the one given; "I've refunded" with no such tool call in the run; one call repeated 3+ times |

**Consequences are not re-blamed.** Rules stop at the first failing node: a
lookup that comes back empty because the step before it passed a bad ID is that
step's failure. The judge follows the same rule.

**Loops.** A hit on an earlier loop visit moves to the node's last visit when
that visit shows the same fault; otherwise the loop self-corrected and the hit
is retired with the `retried` relabel. Known limit: an agent that names the
wrong entity on one turn and never revisits it is retired too.

**Recorder:** each `LLMCallInfo` keeps `output_text` (clipped to 4,000 chars),
read by D11. **CLI:** `argus baseline <run…> --write` — keys, types and value
kinds every healthy run agrees on; never values. **Recorder param:**
`ArgusRecorder(baseline=...)`; without it D5 / D6 / D16 are off.

Two tuning passes came out of the existing tests, not the new suite: D1 first
flagged every extra key (the contextual tests write `noise_a`), then every
near-miss sibling (`noise_a` / `noise_b`). Both would have been false positives
in real code, so D1 now requires the key it meant to be declared.

### How far the 90% travels

90.2% is measured on the suite the rules were designed against, so it is the
optimistic number. On the older suites, which were written before the rules
and never used to tune them (ship_eval s1–s6 and pivot_eval, 86 labelled
faults, 33 healthy runs), ARGUS went from **57% to 58%** (49 → 50 of 86).

That is not the rules failing. It is what those suites contain:

- **Mechanical failures** (tool errors, empty results, dropped fields) were
  already caught at ~88% there, and still are.
- **Semantic failures** are about a third of those suites by design, and 30 of
  the 37 misses. Rules cannot judge meaning.
- Their "LLM" nodes are plain Python with no recorded model call, and there is
  no baseline, so the text, grounding and baseline rules cannot run. Treating
  every node as a model node (a stress mode only) takes held-out semantic
  recall from 0% to 43%.

Honest claim: **about 90% of mechanical silent failures, very little semantic.**
Every fixture was written in-house, so even the held-out number is not
independent. A trustworthy figure needs a blind test: rules frozen, pipelines
and faults built by someone who has not seen them, or real traces with labelled
incidents.

## A whole-pipeline LLM monitor for semantic failures — measured (#149)

> **Superseded by the run reviewer** (last section): the checker + verifier
> measured here is now in `src/argus/review.py`, and the decision below changed —
> a verified finding fails CI when a rule flagged the same step, and stays
> advisory when no rule did. Purposes live in the `argus baseline --purposes` file.

What rules miss is almost all semantic: a total that does not match the charge,
a refund to the wrong order, an approval despite a sanctions hit, an invented
statistic. The judge never sees these; it only reviews steps the rules already
flagged. So a second model was tested that reads the whole finished run and
reports what looks wrong. Same 99 runs throughout (39 true semantic failures,
42 healthy runs), gpt-4.1, two repeats:

| version | semantic failures caught | healthy runs flagged | repeats disagreed |
|---|---|---|---|
| whole trace, fields clipped | 85% | 40% | 5 / 99 |
| full ledger rows, claim by claim | 82% | 36% | 7 / 99 |
| **ledger + one-line purpose per node** | **72%** | **12%** | **3 / 99** |

Reading the ledger instead of a clipped trace barely changed anything; the data
was never the problem. **Telling the model what each node is for** is what cut
false positives. Without it the model calls normal design a mistake, e.g. a
reply step that says "refunded" runs before the refund step, by design. The
purposes were written by the model from a *healthy* run only.

Four of the five remaining flags were real inconsistencies in the test
fixtures' "healthy" variants (a forced output that contradicted its own
evidence, e.g. "2 weeks of therapy" beside `therapy_weeks: 8`), so the true
false-positive rate was about 1 in 42. That is a judgement on a small sample:
promising, not proven.

Quote-verification (code checks every cited value exists in the ledger) did not
help here: it halved recall without reducing false positives, because the model
paraphrases.

**Decision:** build it advisory. Findings appear in `argus show` and the UI and
never change the verdict. Users mark each finding right or wrong; a finding type
earns a place in the CI gate only once its measured precision on real traffic
is high. **Open question, tracked in #149:** where the user writes each node's
one-line purpose (a docstring on the node function, next to `add_node`, one
mapping at the top or bottom of the file, or a draft in the `argus baseline`
file for the team to edit), and what happens for nodes without one.

The eval scripts and results live in the gitignored local suite
(`ship_eval/coverage/`: `monitor_eval.py`, `monitor_verified.py`,
`monitor_v2.py`, `REPORT.md`).

---

## Smaller fixes since the first update

Each is a few lines of code; the *why* is what a contributor needs.

| Issue / ref | Change |
|---|---|
| #146, `67966e89` | **BA-004 refusal wording.** A double-quoted span repeating the node's input is the customer's words and no longer fails the gate. A short decline that cites a number and a reason (refund window, order id) stays a warning; a bare "unable to answer" is still critical. The judge still cannot originate or clear it. This is all of E3 that was fixed |
| E4b, `002340f9` | **A failed accumulator iteration is no longer hidden.** `_apply_loop_retries` keeps the verdict of an earlier iteration that wrote an `operator.add` field (kind `"add"`), so a swallowed timeout on page 1 fails. `add_messages` is still relabelled `retried`, so ReAct recovery and a writer/critic overwrite of `draft` stay clean |
| E5, `51953f84` | **Bracketed placeholders inside prose.** PH-014 was whole-value only. PH-015 now warns on `[Your Name]` / `[TOPIC]` / `[INSERT …]` / `{{var}}` / `{var}` in a sentence, skipping citations, markdown links and `[Draft]`. Promotion to critical is the narrow S3 rule above |
| #148 | **`argus consumers <run>`** lists later nodes handed each written field, as a starting consumer map. It loads nothing and nothing fails CI until someone passes the result as `consumers=`. It cannot see graph-input fields or readers on an untaken branch (S9, S10) |
| #90, `c0a5e465` | `finish` no longer writes `ARGUS_RUN_ID`. One `attach` serves many runs and a process-global pointer graded whichever finished last. Bare `argus check` uses `last`; `ARGUS_RUN_ID` is opt-in for CI |
| #80, #85, #79 | Ledger believes the trace; the judge gets the ledger; replay semantics decided. All written up above |
| #75 | `ArgusWatcher._attach_compiled` keeps `store` / `cache` when recompiling (wrap path) |
| B-2 | No `load_dotenv(override=True)` in library code: an ambient `.env` can no longer replace keys the host app already set. Pinned by an AST scan over `src/argus` |
| B-3 | `add_candidate` compile-validates a regex before queueing it, with the registry's own flags |
| B-4 | A corrupt or non-list `signature_disputes.json` warns (`RuntimeWarning`) instead of reading as silently empty |
| B-8 | Wrap path: a non-callable node runnable (e.g. `RunnableLambda(...) \| tool`) is wrapped, not replaced, so the spec stays valid |
| B-10 | Wrap path: `http_recorder` now has an httpcore backend, so httpx ≥ 0.28 traffic is recorded; a record session that captures nothing logs a warning |
| B-9 / B-14 | Tool calls under an inner chain are re-parented to the node step; `report_tool_call` covers the ones callbacks never see (above) |

B-3 / B-4 / B-8 / B-10 touch the **old** path or shared plumbing. Do not read
them as pivot-path work, and do not extend the wrap path further (see the
`CLAUDE.md` pivot notes).

**Housekeeping.** Full suite is 1,315 tests. Fixtures that need a real model or
a key are gitignored local suites (`pivot_eval/`, `ship_eval/`); they are not in
CI, so a green CI is the tracked matrices and unit tests only.

---

## The run reviewer: two checks must agree (#149)

**In one paragraph.** Every rule is now either **strict** (nothing healthy produces
it; fails CI on its own, as before) or **heuristic** (usually a failure, sometimes the
design). With one-line node purposes, an LLM reviewer reads the whole ledger once per
run and verifies what looks wrong. A heuristic hit fails CI only where the reviewer
verified the same step; a rule *warning* the reviewer verified now fails CI; a verified
item with no rule signal is advisory, unless a second, different model verifies it too
(added later the same day; see the last sections). The reviewer never clears a strict
fail. No purposes → it does not run and the rules decide alone.

### Why: a blind probe

Every fixture before this was written alongside the rules it tested. The "0 false
positives on 63 healthy runs" figure was never checked against pipelines the rules had
not seen, which the "How far the 90% travels" section already warned about. So three
new pipelines were written without reading the rule code: a refund support agent
(classify → lookup_order → check_policy → issue_refund → draft_reply), a RAG report
(plan → search → rerank → write), and a nightly CRM scoring job with `Send` fan-out.
They use real `@tool`s and a chat model invoked inside nodes without passing `config`,
the way most teams write it. 29 faults, 11 healthy runs. They live in the gitignored
`blind_eval/` (`probe.py`).

Rules alone (consumer map + baseline, judge off) failed **6 of 11 healthy runs**:

| false alarm | rule | why it fired |
|---|---|---|
| "I've approved a refund of $94.99" after `create_refund` succeeded (3 runs) | D14 | matched the verb stem `appro` against tool names |
| FAQ path: `draft_reply` reads `order`, which only the refund path writes | contextual | "never written → blame the first step" knows nothing of branches |
| web search returned `has_more: true`; the node kept the top page | D4 | top-k *is* the design for search |
| existence check: report lookup → 404 → "not sent yet" | inspector | a 404 is the answer |

And it passed five faults: `Hi {customer_name}, …` in the customer reply and a report
that only repeated the question (both only *warnings*), "I've processed your refund"
with no refund call ("processed" was not a D14 verb), a total computed over 1 of 3 rows,
and a date-format change that made the policy wrongly decline a customer.

The per-step judge, on with a key, changed nothing. It was asked twice in 40 runs and
said FAIL both times, correctly. On the template reply CI still passed, with the finding
reading "no rule agreed; not gating", although PH-015 had flagged that same step: the
judge can only drop a warning, and warnings never fail CI.

The #149 checker + verifier with node purposes, on the same runs, flagged none of
the healthy runs and verified all four semantic misses. But it varies run to run, and on
tool failures it points at the downstream victim (`draft_reply`, not `lookup_order`, for
a swallowed 500). Each is good at what the other is bad at, so now each checks the other.

### What landed

**Strict vs heuristic** (`review.HEURISTIC_RULES`, `review.is_heuristic`). Heuristic:
D4 `unfollowed_pagination`, D6 `sentinel_value`, D12 `ungrounded_number`, D13
`near_miss_identifier`, D14 `unperformed_action`, D15 `stuck_loop`, an `error_response`
whose evidence says HTTP 404, and the contextual "never written" guess. Everything else
critical is strict.

**The reviewer** (`src/argus/review.py`, `Reviewer`). The prompts are the
`ship_eval/coverage` checker v3 and verifier, **verbatim** (see the warning in the
file). One checker call over the ledger: each step's input, update, tool calls and model
output, plus the purposes. Then one verifier call per reported item, at most 8. The
verifier works closed-world and must name the exact correct value, and arithmetic is a
formula that `_safe_eval` evaluates. Transport is `llm_proxy` (BYOK, then hosted),
`gpt-4.1` by default. **Any call that fails → `Review(ok=False)` → the run is graded by
the rules alone**, so an outage can never quietly turn heuristic fails into passes.

**Where it runs** — `grading.finish`, now:

```text
ledger → reviewer (reads only the ledger, so it can go first)
       → settle_step_signals   (a step whose only critical evidence is an unverified heuristic → pass)
       → run_rules(keep=)      (a rejected heuristic hit is not an origin; the scan goes on)
       → split_hits            (rejected hits filed as warnings: "… the run reviewer did not confirm it")
       → contextual            (a "never written" guess stands only if the reviewer verified its
                                origin or a declared reader of the field; else warning `missing_field_guess`)
       → confirm_warnings      (verified + rule warning → `review_confirmed`, critical; notes on NodeEvent.review)
       → finalize
```

| the rules say | reviewer verified that step? | `argus check` |
|---|---|---|
| strict failure | either | fail |
| heuristic failure | yes | fail |
| heuristic failure | no | pass (warning kept) |
| a warning | yes | **fail** (`review_confirmed`) |
| nothing | yes, and a second model (`o4-mini`) verifies it cold | **fail** (`review_verified`) — added later the same day, see the last section |
| nothing | yes, one model only | pass, advisory finding |

Agreement is per **step**, not per finding kind: a verified item on `draft_reply`
confirms a D14 hit on `draft_reply` whatever the item says. That is what was measured.

**Turning it on.** `argus baseline <healthy runs> --purposes --write argus.baseline.json`
drafts a `purposes` block next to the baseline (one LLM call per run); the team edits it.
`ArgusRecorder(baseline=...)` picks it up, or pass `purposes={...}` directly. The
reviewer runs when there are purposes and an LLM path. `review=False` turns it off,
and `review=True` without purposes raises. When it runs, the per-step judge does not,
because the judge would drop the very warnings the reviewer needs to promote. This
settles the #149 open question of where purposes live: in the baseline file, drafted
from a healthy run and edited by the team.

**Findings.** `NodeEvent.review` holds each verified item with a role (`confirms` /
`promoted` / `advisory`), and the run file round-trips it. `collect_findings` emits them
as `review_<kind>` (source `llm`): critical when promoted, warning otherwise. `argus show`
prints them in a **Run reviewer** panel; advisory notes are visible nowhere else.

### Deterministic fixes that came with it (these apply with no reviewer too)

- **D14** matches the verb *and the object* against tool names. "I've approved a refund"
  is satisfied by `create_refund`. "I've processed your refund" with no refund call is a
  hit. A generic verb with no object nearby ("I've processed it") is skipped. This
  removed 3 of the 6 blind false alarms with no reviewer at all.
- **D2** names the HTTP code in its evidence (`(HTTP 404)`). Without it, a 404 the
  reviewer settled at the inspector was re-detected by D2 and failed anyway.
- **Contextual "never written"**: the finding carries `confidence=GUESS_CONFIDENCE` (0.5),
  so the reviewer can tell a guess from a drop. When a step before the reader already
  failed, the guess lands on that step, not on the first row. In the blind CRM run,
  GraphQL `errors` on `list_accounts` meant nothing to fan out, and `check_existing`, a
  bystander, was blamed for `scored`. `{"readers": [...], "required": False}` declares a
  field read on some paths only: never written is fine, dropped or blanked still fails.
- `run_rules(keep=)`: a rejected hit no longer stops the scan, and the next rule on the
  same step runs too.

### Tried and rejected

- **"The baseline's writers of this field never ran, so it is a branch."** This fixed the
  FAQ false alarm and hid three coverage-suite faults. When `plan` writes
  `queries: []`, no worker runs and `notes` is never written; the absent writers *are*
  the failure. Removed. The FAQ case is `required: False` or the reviewer.
- **"Pagination with a stated total larger than the page is strict."** Search APIs state
  `total: 1532` and return the top 10. D4 stays heuristic, so a CRM pull that stops at
  page one now fails only if the reviewer verifies it (below).
- **Deferring "never written" entirely when an upstream step failed.** This lost the
  "`docs` was never written" reason on the failing step
  (`test_contextual_reason_preserves_existing_tool_message`). The guess is retargeted
  instead.
- **Editing the prompts.** Adding `{var}, ` to the checker's template examples turned the
  Salesforce page-one case from caught 2/2 into 0/2. The texts stay verbatim.

### Measured

Live, `gpt-4.1`, one run each, consumer map and baseline on for the blind probe, judge
off in the rules-only column (`blind_eval/validate_review.py all`).

**Blind probe** (29 faults, 11 healthy; nothing here was tuned on it before today):

| | caught, right node | healthy runs failed |
|---|---|---|
| rules, before today | 22 | 6 |
| rules, with today's deterministic fixes (no LLM) | 23 | 3 |
| rules + reviewer | **24** | **0** |

Gained: the `{customer_name}` reply, and the report that echoes the question (both a
rule warning plus a verified item). Removed false alarms: FAQ branch, web-search
`has_more`, 404 existence check. Lost: the Salesforce page-one truncation (heuristic D4;
the verifier confirms it about half the time). One healthy run carries an advisory note.

**Held out: ship_eval s1–s6 + pivot_eval p8–p12** (86 labelled faults, 29 truly healthy
runs; not used to choose which rules are heuristic):

| | mechanical (57) | meaning (29) | all (86) | healthy runs failed (29) |
|---|---|---|---|---|
| rules alone | 50 | 0 | 50 (58%) | 3 |
| rules + reviewer | **54** | 1 | **55 (64%)** | 3 |

Lost: none. False alarms added: none. The 3 healthy failures are the same with and
without the reviewer: three KYC fixtures that never declare `allow_empty` on a clean
sanctions screen, a config gap that predates this change, on a strict rule. Of the 28
meaning faults still missed, **23 carry an advisory note on the right step** (a wrong
service blamed, a rollback aimed at staging, an invented therapy history): visible in
`argus show`, not gating. Advisory notes landed on 2 of the 29 healthy runs. Of the 4
"healthy" fixtures whose own evidence contradicts their output, the reviewer failed 1.

One fix came out of this run. The ship_eval travel fault `supervisor_skips_payment`
(the supervisor routes past the payment agent, so `payment` is never written) was first
*lost*. The reviewer had verified `itinerary` ("Total charged: $0.00"), the starved
reader, not the supervisor the guess blames. A "never written" guess now counts as
confirmed when the reviewer verified either the step it blames or a declared reader of
that field. With that change it is caught again, and the FAQ false alarm stays gone.

Run-to-run variance is real. Treat a ±1–2 swing on a re-run as noise, not a regression.

### Final check: four fresh pipelines, frozen before the first run

Written after everything above landed, with nothing tuned on them: IT onboarding
(Okta-style provisioning + welcome email), accounts-payable invoices (extract →
3-way match → payment → vendor notice), BI Q&A (SQL → warehouse → analysis →
answer), and content moderation with a subgraph. 25 faults; 21 healthy runs, 12 of
them with **real gpt-4o-mini** in every model node. Purposes were drafted by
`draft_purposes` from one healthy run and **not edited**. The only changes after
freezing were fixture fixes, and none of them changed a scenario's intent
(`blind_eval/final_probe.py`, `final_score.py`).

| | zero-config | consumers + baseline | + reviewer |
|---|---|---|---|
| mechanical (9) | 5 | 6 | 6 |
| model-output shape (5) | 1 | 1 | **4** |
| meaning (11) | 3 | 3 | 3 |
| **all faults (25), right node** | 9 | 10 | **13** |
| healthy failed, scripted (9) | 3 | 2 | 1 |
| healthy failed, real gpt-4o-mini (12) | 4 | **6** | **0** |

10 of the 12 faults still missed carry a verified advisory note on the right step:
paying 10× over the PO, paying the wrong vendor's bank account, approving a partial
delivery, removing a clean post, a misread start date. 2 are missed outright: a
BigQuery `jobComplete: false` partial result, and a classifier that answered in prose,
so the node fell back to "not toxic".

What it found:

- **D12 is the false-alarm engine on real model prose.** All 6 real healthy runs that
  rules alone failed were `ungrounded_number` on a model rewording or computing a
  figure. The reviewer cleared every one. Without a reviewer, D12 on model prose is
  noisy.
- **A 409 "already exists" in an idempotent re-run** fails as a strict
  `error_response`. That is the one remaining false alarm with the reviewer on. A
  candidate for the heuristic list, like 404.
- **Vocabulary gaps** a rule could close: an SMTP relay's `rejected: [to]`, BigQuery
  `jobComplete: false` + `pageToken`, a payment `status: requires_approval`.

**All three suites together** (140 faults, 61 healthy runs; KYC config-gap fixtures included):

| policy | faults caught, right node | healthy runs failed |
|---|---|---|
| rules alone | 83 (59%) | 14 (23%) |
| rules + reviewer (what ships) | **92 (65%)** | **4 (7%)**, 3 of them the KYC `allow_empty` gap |
| … and a verified note alone also fails CI (not built) | 126 (90%) | 7 (11%) |

The last row is the open decision. The notes are right often enough to be worth
gating on, but not yet often enough: 3 of 61 healthy runs carried one. The
measured idea worth trying next is two independent verifier calls that must both
confirm (`ship_eval/coverage`: 86% recall, 1 false flag in 29).

### Limits, honestly

- **Run-to-run variance.** The reviewer is an LLM at temperature 0, which is still not
  deterministic. On the Salesforce page-one case the checker reported the problem 2/2
  times and the verifier confirmed it 1/2. A heuristic-only failure is caught *most*
  of the time, not always. A strict failure is unaffected.
- **Blame on the victim.** When a scraper returns documents with empty `content`, the
  run fails on `write` (invented numbers, verified), not on `search`. The gate is right
  and the origin is wrong.
- **Decisions against the evidence** with no rule signal on that step (the date-format
  case) are advisory at best.
- **Cost and latency.** About 1 + N gpt-4.1 calls per run (N ≤ 8 reported items), made
  when the graph finishes, in the thread that ends the run, so `invoke()` returns
  seconds later. Meant for CI and pre-deploy runs, not every production request.
- **Purposes are load-bearing.** Bad purposes mean a noisy reviewer. Without purposes it
  is off by design.
- **No live-model test in CI.** `tests/test_review.py` stubs the transport. The live
  numbers above come from `blind_eval/validate_review.py` (gitignored, needs a key).
- **Not independent.** The blind probe was written by the same team, with the same model
  family, that built the rules. It is better than tuning on the test set, but it is not
  a third-party eval or labelled production incidents.

```bash
PYTHONPATH=src pytest tests/test_review.py tests/test_contextual.py tests/test_trace_rules.py -q
PYTHONPATH=src python blind_eval/validate_review.py all     # live: needs OPENAI_API_KEY (reads .env)
```

---

## Later on 3 Oct: the two-model gate, new vocabulary, and `pytest --argus` on the recorder (#78)

The final check left two things on the table. Wrong decisions with a clean shape were
seen (a verified advisory note on the right step) but never blocked. And three vendor
shapes were missed outright. Both are fixed, along with #78.

### What changed

- **A verified finding with no rule behind it can now fail CI**, but only when a second,
  *different* model verifies the same item cold (`Reviewer.second_model`, `o4-mini`). The
  first verifier is gpt-4.1. Both must confirm → critical `review_verified`, role
  `two_models`. One confirms → advisory, as before. A second-model error counts as "no",
  never as a failed review: this path can only add a fail. `providers.call_openai` now
  sends reasoning models `max_completion_tokens` and no `temperature`.
  Measured basis: `ship_eval/coverage`, gpt-4.1 + o4-mini both agreeing (86% recall,
  1 false flag in 29).
- **409** is a judgement call only when the tool says the thing already exists or is a
  duplicate (`review._says_already_exists` reads the tool body). An idempotent create
  that answers "User already exists" is not a failure. "Application is not in a stage
  that can be advanced" is a real conflict and stays strict. The first version made
  every 409 heuristic and lost ship_eval `ats_409_on_advance`, which is how we found this.
- **D2** counts a mail relay that refused every recipient (`accepted: []`,
  `rejected: [...]`).
- **D4** splits in two. Cursors and `has_more` stay `unfollowed_pagination` (heuristic). An
  explicit "unfinished" flag (`jobComplete: false`, `incomplete_results: true`) is the
  new strict `incomplete_result`.
- **D17 `status_overstated`** (heuristic): a tool said `requires_approval` /
  `pending_*` and the node's update records a done word (`scheduled`, `succeeded`, …).

### Measured: before vs after, live, same purposes, all three suites

| | faults caught, right node (140) | healthy runs failed (61) |
|---|---|---|
| rules alone, before | 83 (59%) | 14 |
| rules alone, after | 86 (61%) | 12 |
| rules + reviewer, before | 92 (66%) | 4 |
| **rules + reviewer, after** | **125 (89%)** | **4** |

Per suite (rules + reviewer, after): blind 26/29 with 0/11 healthy failed; held-out
(ship_eval + pivot_eval) 77/86 with 4/29; final 22/25 with 0/21, 12 of those with real
gpt-4o-mini.

The 4 healthy failures are not new false alarms from this change. Three are the KYC
fixtures that never declare `allow_empty` on a clean sanctions screen: a strict rule,
the same with and without a reviewer, a config gap. The fourth is
`sql:healthy_empty_result_is_the_answer`, which two models flagged because its chart
node is hardcoded to a month/revenue bar chart and emitted one for a question about
refunded orders. That is arguable, and it is counted here as a false alarm. The 4
ship_eval "healthy" fixtures whose own evidence contradicts their output (excluded from
the counts, as before) are now all flagged.

**Still missed (15):** two format-drift decisions in the blind refund pipeline (a total
string and a date string changed shape, and a wrong decision followed); wrong decisions
that only one of the two models confirmed (wrong vendor bank account, approving an
uncovered peril, a disqualified lead emailed, a risk tier against its score, a
wrong-jurisdiction playbook, a letter amount that differs from the decision); two tool
failures with a plausible fallback (NetSuite 500 recorded as posted, FX error falling
back to parity); a diagnosis that gave up when the evidence was there; a "resolved"
claim before verification; a classifier that answered in prose; and blame landing on
the victim (empty scraped content blamed on `write`; the wrong top region blamed on
`answer`, which repeated it).

**Honest caveats.** Held-out is no longer strictly held out: the 409 narrowing and the
reader corroboration earlier came out of held-out rows, but both *restore* a catch and
neither was tuned to gain one. A finding with no rule behind it now fails CI on two LLM
calls; keep watching the healthy-run rate on real traffic. It costs one extra o4-mini
call (about 7 s) per verified item that no rule backs. Variance is real: a ±2 swing on a
re-run is noise.

## `pytest --argus` on the recorder (#78)

`pytest --argus` records every LangGraph run in the session with **nothing patched**.
`argus.pytest_instrument` registers one handler through LangChain's public
`register_configure_hook`, which adds it to every runnable invoked in the process.
The handler (`_AutoRecorder`) runs inline (`run_inline = True`). At a graph's
root `on_chain_start` the compiled graph is therefore on the call stack, inside
`Pregel.stream` / `astream`. We checked this holds for invoke, stream, ainvoke, astream
and batch. The handler binds an `ArgusRecorder` to that graph with the new
`ArgusRecorder.bind(app)`: `attach()` without the binding, so the same topology,
reducers and subgraph handling. It then forwards that run tree's callbacks. This answers
the issue's open question: the topology is read from the graph itself, not
reconstructed from the callback stream.

- **No double recording.** `attach()` now tags its runs with `metadata["argus_recorder"]`, and the
  hook skips them. An `ArgusWatcher` app (`_argus_auto_persist`) is skipped too.
- **Threads a test starts itself** are covered: the hook's contextvar *default* is the
  handler, so a thread that did not inherit the context still sees it.
- **A graph composed under another runnable** (`RunnableLambda(...) | app`) is found
  when the graph itself starts. A plain chain with no graph is ignored.
- LangChain has no way to unregister a hook, so `uninstall_auto_instrumentation()`
  switches the handler off (`active = False`).

Done-when criteria from the issue: `tests/test_argus_ci_gate.py` forces
`argus.patcher.patch_graph` to raise and passes under `--argus`; `tests/test_pytest_plugin.py`
is green, including `test_reinstall_then_uninstall_leaves_nothing_patched` and a new
`test_install_replaces_nothing_on_langgraph`; nothing on `StateGraph` or `Pregel` is
replaced. #83 (deleting the wrap path) is no longer blocked by this; replay continuing
the tail still blocks it.

```bash
PYTHONPATH=src pytest tests/test_argus_ci_gate.py --argus -q
PYTHONPATH=src pytest tests/test_pytest_plugin.py tests/test_review.py tests/test_trace_rules.py -q
PYTHONPATH=src python blind_eval/validate_review.py all && PYTHONPATH=src python blind_eval/final_probe.py   # live, reads .env
```
