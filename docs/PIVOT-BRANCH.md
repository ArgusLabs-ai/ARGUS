# Pivot branch — contributor update

Branch: **`pivot/fat-traces`**  
Last updated: 15 Sep 2026 (third update: `argus ingest langsmith` — grade a trace file with no app).

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
`BA-005 structural malformation` fires on any flat dict, which is what a normal
LangGraph node returns, so counting it would let the judge fail almost anything.

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
| **A silent early iteration of a loop** | `_apply_loop_retries` relabels every earlier iteration `retried` when the final one passes, and the gate skips `retried`. Right for a genuine retry, wrong for an accumulating field where round 1's empty contribution is never superseded (a pagination loop that loses page 1 grades clean). Parallel `Send` workers are no longer caught by this (E4, below); the sequential case is **E4b (#131), still the open product decision** — "never retry a reducer write" would break a ReAct agent that recovers from a tool error, whose `messages` field accumulates too |
| **Custom reducers, at fan-in** | Still round-trip as `"overwrite"` (`ledger.reducer_kinds`), and no single successor's recorded input can show what a merge did, so a custom fan-in reads as the last branch winning. The *sequential* case is fixed — see "The notebook believes the trace" below |
| **`add_messages` id de-duplication** | Folded as plain concatenation, so an updated message counts twice. Pinned by a test |
| **Token accounting** | `llm_tracker` reads usage off the node's output dict, so a node returning `{"category": "..."}` records none. Verified identical on the old wrap path — pre-existing, not a pivot regression. `on_llm_end` would fix it on this path |
| **A victim flagged alongside the origin** | When an upstream `{}` starves a downstream model node, both are flagged. `first_failure_step` is still the origin, so the verdict is right and the extra finding is noise: `degraded_input` covers present-and-bad fields, not absent ones |
| **A node that echoes its input** | The one true-positive miss: a researcher branch returned the question verbatim as its note and the run graded clean. Echo detection exists for main answer fields but not for a fan-in accumulator. Related signal worth adding: the reviewer loop hit its revision cap and shipped anyway, which is itself evidence |
| **`BA-005 structural malformation`** | Warning-level noise on any flat dict — i.e. on most healthy nodes. It no longer gates anything (see defect 10) but still clutters `argus show` and the `argus fix` prompt |
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
