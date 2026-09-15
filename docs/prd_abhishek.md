# prd_abhishek — traces in, verdict out

Branch base: `pivot/fat-traces` at `9eea6c9`. Owner: Abhishek. Governed by /mystandard.
Status: draft, grilled 2026-09-14, rebased on `9eea6c9` the same day.

## The one-line point

The pivot branch stops wrapping the engine, but it still needs the user's live
app in the process (`ArgusRecorder` is a LangChain callback). This PRD adds the
other half of the brief: **read a trace file that a framework already emitted,
grade it with the same rules, fail `argus check`.** No app, no LangGraph import
at grade time.

## Today / instead

**Today:** `ArgusRecorder().attach(app)` rides LangGraph callbacks and grades
one run. A LangSmith export, an OTel file, or a run recorded on another machine
cannot be graded. `argus` has no `ingest` command.

**Instead:** `argus ingest langsmith runs.jsonl` writes a RunRecord into
`.argus/runs/` and `argus check last` fails on the origin node — the same
ledger → contextual → inspector → judge-last chain the recorder uses.

## Acceptance (whole effort)

WHEN a LangSmith export of a LangGraph run in which one node returned `{}` is
passed to `argus ingest langsmith` THEN `argus check last` SHALL exit 1 and
name that node, with no LangGraph app loaded.

## Evidence this is the right shape (verified 2026-09-14, langgraph 1.2.11)

- Each node's `on_chain_start` metadata carries `langgraph_node`,
  `langgraph_step`, `langgraph_triggers`, `langgraph_path`,
  `langgraph_checkpoint_ns`; tag `graph:step:N`.
- `on_chain_end` outputs for a node is the **update** the node returned
  (`{}` for a silent node), not the merged state.
- LangSmith's tracer is that same callback, so a LangSmith Run has
  `extra.metadata.langgraph_node` and, for a node that returned keys,
  `outputs` == update. **A node that returned `{}` does not arrive as `{}`**
  (BUG-1 below): its end-PATCH carries no `outputs` at all. Run fields:
  `id`, `trace_id`, `parent_run_id`, `name`, `run_type`, `inputs`, `outputs`,
  `error`, `extra`, `start_time`, `end_time`, `dotted_order`, `tags`.
- Edges are **not** in the trace. `langgraph_triggers` reads
  `branch:to:<self>`; it does not name the source. Edges need a sidecar.
- OTel GenAI spans: `gen_ai.input.messages` / `gen_ai.output.messages` are
  Opt-In. Default OTel is a skinny trace. Not this milestone.
- The pivot branch has zero file-ingest code (at `9eea6c9`, `ingest` appears in
  `src/argus` only in docstrings and a node name in `cmd_show.py`).
- At `9eea6c9` the full suite is 1018 passed, 4 skipped, 2 xfailed in ~15s on
  langgraph 1.2.11 (embeddings off by default). Every step may run it.
- Crash blame needs no live exception: `crash_origins` reads the missing key
  out of `NodeEvent.exception` text with a regex (`KeyError: 'x'` /
  `KeyError('x')`). A LangSmith `error` string can feed it (S-11).

## Bugs found

### BUG-1 — "silent node exports `outputs == {}`" was wrong (found during S-1, 2026-09-14)

**Seen:** stub-client probe of `demo/fat_trace/demo_graph.py` under
`LangChainTracer` (langsmith 0.12.4, langchain-core 1.6.3). Every chain run's
create-POST carries `outputs={}` (nothing has run yet). `summarize`'s end-PATCH
carries `outputs=None`, because `RunTree.patch` sends
`self.outputs.copy() if self.outputs else None`, and `Client.update_run` drops
`outputs` when it is `None`. `search` and `answer` end-PATCHes carry their real
updates.
**Also seen:** with `hide_outputs=True` (or `LANGSMITH_HIDE_OUTPUTS=true`),
`Client._hide_run_outputs` returns `{}` for every run, root included. So a
single node's empty outputs cannot tell "returned `{}`" from "hidden".
**Reproduce:** run the S-1 script and inspect the `summarize` row; or read
`langsmith/run_trees.py` `RunTree.patch` and `langsmith/client.py`
`Client.update_run`.
**Spec change:** a node run with no error and `outputs` absent or `{}` is an
empty update **only if** the root run's `outputs` is a non-empty dict (the
merged final state proves outputs were not hidden). Root `outputs` absent or
`{}` → skinny, refuse. S-1, S-3 and S-6 updated.
**Not verified:** what the LangSmith server stores for a create `{}` followed
by a PATCH without `outputs` (null or `{}`). Both are handled by the rule
above. One real export of the demo graph closes this (needs Abhishek's
LangSmith account; not a step).

## Options considered

| | Option | Verdict |
|---|---|---|
| A | `argus ingest langsmith <jsonl>` → RunRecord, reuse recorder's grading chain | **Build.** Fat by construction; matches the callback the recorder already trusts |
| B | OTel/OTLP ingest | Later. Payloads Opt-In; would mostly hit the "refuse to grade" path |
| C | Keep only `ArgusRecorder` and add `argus export` | No. That is library thinking; the recorder stays as the fat-recorder fallback |

Decision: A. Native ARGUS trace format is the RunRecord JSON that already
exists in `.argus/runs/`; ingest is an adapter that produces one.

## Reused from the harness work (`~/argus-agents-lab/adapters/claude_code/`)

- `trace_reader.py` → `ingest.py` → `save_run` shape. Same pattern here.
- The harness ingest built raw `NodeEvent`s and **skipped the inspector**.
  Do not repeat that: rows must go through `ArgusSession.on_node_start` /
  `on_node_end` so inspection runs (S-2 makes that reusable).
- The "truncation" detector ports to LLM rows (S-8). "Ignored tool error" no
  longer needs porting: upstream `9eea6c9` grades a raised tool as critical
  (`inspect_tool_calls`), which covers it (S-9 retired).

## Parked — Varad decides, steps must not touch

- Empty corpus (`retrieve` legitimately returns `[]`): pass or fail.
- Silent early loop iteration relabelled `retried`, which the gate skips.
- Type drift (`consumers` growing a shape).
- A tool that raised and was then retried successfully by a later step is
  still critical under upstream `inspect_tool_calls` (per-step, no look-ahead).

## Must not (all steps)

- No push to `ArgusLabs-ai/ARGUS` and no PR until Abhishek has read the diff.
- No LLM call in any test; judge paths mocked (memo D-04 budget).
- No real trace content committed; fixtures are generated from `demo/`.
- Do not delete or modify `patcher.py` / `watcher.py` / `pytest_instrument.py`.
- Never set `ARGUS_EMBEDDINGS=1` in ingest code or tests: it POSTs node output
  values to OpenAI (opt-in since upstream `9eea6c9`).

## Steps

### S-1 — Offline LangSmith-shaped fixture from the demo graph

**PR:** one.
**Depends on:** nothing.
**Files:** `scripts/make_langsmith_fixture.py` (new),
`tests/fixtures/langsmith/demo_graph.jsonl` (new, generated, committed).
**Today:** no LangSmith-format sample exists in the repo. Tests cannot exercise
a file path without a LangSmith account.
**Change:** run `demo/fat_trace/demo_graph.py`'s graph under
`langchain_core.tracers.langchain.LangChainTracer` with a stub
`langsmith.Client` that captures every run payload the tracer posts
(langsmith 0.12.4 calls `create_run` and `update_run` directly when a client
is passed; stub those plus `flush`). Merge create then update per `id`, with
the client's own rule: a key whose value is `None` is not sent, so it never
overwrites. Write one JSON object per line with at least: `id`, `trace_id`,
`parent_run_id`, `name`, `run_type`, `inputs`, `outputs`, `error`, `extra`,
`start_time`, `end_time`, `dotted_order`, `tags`. No network.
**Acceptance:** WHEN the script runs THEN it SHALL write a JSONL whose `chain`
runs include one with `extra.metadata.langgraph_node == "summarize"` whose
`outputs` is absent or `{}`, one for `search` whose `outputs` has `docs`, and
one root run with `parent_run_id == null` whose `outputs` is a non-empty dict
(BUG-1).
**Verify:**
```
PYTHONPATH=src python scripts/make_langsmith_fixture.py
python - <<'PY'
import json;rows=[json.loads(l) for l in open("tests/fixtures/langsmith/demo_graph.jsonl")]
s=[r for r in rows if (r.get("extra") or {}).get("metadata",{}).get("langgraph_node")=="summarize"]
assert s and not s[0].get("outputs"), s
root=[r for r in rows if r["parent_run_id"] is None]; assert len(root)==1 and root[0]["outputs"], root
print("ok", len(rows))
PY
```
**Must not:** call the network; import `langsmith.Client` for real; touch `src/`.

**Done 2026-09-14 (local branch `s1-langsmith-fixture`, not pushed).** Learned:
the probe found BUG-1 before any ingest code existed. Two more shapes an
ingester must expect: every field is present and `null` when never sent (the
root's `parent_run_id` is `null`, not absent), and a silent node's `outputs`
is `{}` only because the create-POST sent `{}` first. The root run carries no
`langgraph_node` and no tags. Ids and timestamps change on every regeneration,
so tests must not pin them. No proof-by-breaking: this step adds data, not a
guard.

### S-2 — Extract the recorder's grading chain so a file can use it

**PR:** one.
**Depends on:** nothing.
**Files:** `src/argus/grading.py` (new), `src/argus/recorder.py`.
**Today:** `ArgusRecorder._new_session()` and `ArgusRecorder._finish(session,
root)` build the session (placeholder node registry, edges, reducers, judge
flag, `_defer_auto_finalize`) and run refuse-if-unfinished → ledger →
contextual (`_blame_origins`) → disable investigate → finalize → set
`ARGUS_RUN_ID`. Only the recorder can call them.
**Change:** move them to module functions `new_session(node_names, edge_map,
conditional_sources, reducer_fields, *, judge, validators, strict,
max_field_size)` and `finish(session, consumers, unfinished=())` in
`grading.py`. `IncompleteTraceError`, `_placeholder_node`, `_blame_origins`
and `_refuse` move with them. The recorder still computes `unfinished` from its
own `_pending` / `_root_of` and passes it in. `_topology()` (now a 4-tuple with
subgraph parents) and the subgraph-parent skip stay in `recorder.py`: they
need the app. Recorder calls the new functions; behaviour identical.
**Acceptance:** WHEN the pivot suites run THEN they SHALL pass unchanged, and
`grading.py` SHALL import nothing from `langgraph` or `langchain_core`.
**Verify:**
```
PYTHONPATH=src pytest tests -q          # pass = 1018 passed, 4 skipped, 2 xfailed (as at 9eea6c9)
grep -nE "^(from|import) (langgraph|langchain)" src/argus/grading.py ; echo "expect no output above"
```
**Must not:** change any assertion in existing tests; change detection rules.

**Done 2026-09-14 (local branch `s2-grading-module`, not pushed).** Learned:
the fourth argument is `reducer_fields` (the reducer callables), not
`reducer_kinds` — the session merges running state with the callables and
derives the kinds itself; the step text is corrected above. Two tests call the
recorder's private names (`recorder._finish(recorder._new_session(), ...)`,
`from argus.recorder import IncompleteTraceError`), so the recorder keeps thin
`_new_session` / `_finish` wrappers and re-exports the error; `run_ids` stays
on the recorder. `argus.grading` imports cleanly with `langgraph` /
`langchain_core` blocked, so S-3 can run without them. Proof by breaking:
removing the empty-trace refuse from `grading.py` fails
`test_recorder.py::test_an_empty_trace_refuses_to_grade` and
`test_silent_failure_matrix.py::test_a_skinny_trace_is_refused_rather_than_graded_clean`
(same guard, direct and end to end); restored, 1018 pass.

### S-3 — `argus ingest langsmith <file>` grades a run with no app

**PR:** one.
**Depends on:** S-1, S-2.
**Files:** `src/argus/ingest/__init__.py` (new), `src/argus/ingest/langsmith.py`
(new), `src/argus/cli/cmd_ingest.py` (new), `src/argus/cli/main.py`,
`tests/test_ingest_langsmith.py` (new).
**Today:** no `ingest` command.
**Change:** read JSONL; merge rows by `id`; rebuild the tree by
`parent_run_id`; select `run_type == "chain"` runs whose
`extra.metadata.langgraph_node` is set and whose `tags` contain a
`graph:step:N` tag (this excludes inner `seq:step:N` runnables). Order by
`langgraph_step` then `dotted_order`. For each: `session.on_node_start(node,
capture_state(inputs))` then `on_node_end(node, input_snap, update,
duration_ms, exc=...)`, where `update` is `capture_output(outputs)` when
`outputs` is a non-empty dict, and `{}` when `outputs` is absent or `{}` and
the run has no error (BUG-1: that is how a silent node arrives; S-6 refuses the
trace first if outputs were hidden),
where `exc` is `None` when `error` is empty, else an exception whose `str()` is
the run's `error` string verbatim (crash blame reads that text; see S-11).
Skip a node run that has another selected node run among its descendants:
that is a subgraph parent, and its outputs are the subgraph's merged state
(upstream `9eea6c9` skips the same row in the recorder, for the same reason).
`node_names` = the set kept. `edge_map` = `{}` for now; `has_successors` for
the `empty_output` rule = "a node with a higher `langgraph_step` exists in
this trace". Root run `inputs` → `session.capture_state` as initial state.
Call `grading.finish`. Save. Print the same one-line verdict the recorder
prints. Respect the cloud-sync guard: refuse when `is_logged_in()` unless
`--allow-cloud` (memo gotcha).
**Acceptance:** WHEN `argus ingest langsmith tests/fixtures/langsmith/demo_graph.jsonl`
runs THEN `argus check last` SHALL exit 1 with `summarize` as
`first_failure_step`, with `langgraph` not imported by the ingest module.
**Verify:**
```
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q
PYTHONPATH=src python -c 'from argus.cli.main import app; app()' ingest langsmith tests/fixtures/langsmith/demo_graph.jsonl
PYTHONPATH=src python -c 'from argus.cli.main import app; app()' check last; echo "exit=$?"   # expect exit=1, summarize
grep -nE "^(from|import) (langgraph|langchain)" src/argus/ingest/langsmith.py ; echo "expect no output above"
```
**Must not:** import the user's app; write `node_fn_refs`; upload to cloud
while logged in without `--allow-cloud`.

**Done 2026-09-14 (local branch `s3-ingest-langsmith`).** Learned:
`python -m argus.cli.main` exits 0 and does nothing (no `__main__` guard), so
the verify commands above now call `app()` directly; run them from a scratch
directory, or they write `.argus/` into the repo. `edge_map = {}` would never
fire `empty_output` (successors come from edges), so ingest builds edges from
observed step order — each node → the nodes at the next step — which is the
"a later step exists" rule in session terms; S-5 replaces it with real edges.
The one-line verdict is printed by the session as it saves, not by the
recorder, so ingest prints nothing extra. The judge is off for ingest (a file
spends nothing). Proof by breaking: removing the cloud guard fails
`test_logged_in_refuses_to_save_without_allow_cloud`; passing `{}` edges fails
`test_the_silent_node_is_blamed_from_the_file_alone`; restored, 1022 pass.

### S-4 — Tool child runs land on the ledger row

**PR:** one.
**Depends on:** S-3.
**Files:** `src/argus/ingest/langsmith.py`, `scripts/make_langsmith_fixture.py`,
`tests/fixtures/langsmith/tool_graph.jsonl` (new), `tests/test_ingest_langsmith.py`.
**Today:** `run_type == "tool"` runs are dropped; `LedgerRow.tools` is empty
on ingested runs, so a swallowed HTTP 500 in a tool result is invisible.
**Change:** for each selected node run, collect descendant runs with
`run_type == "tool"` → `[{"name", "input", "output", "error"}]` (the dict
shape `recorder.on_tool_start` builds) and pass it as
`on_node_end(..., tool_calls=tools)`. Never attach tools to the event after
`on_node_end`: graders run inside it, so late tools are recorded and never
read (upstream #86, fixed in `9eea6c9`). Confirm in the S-1 fixture whether a
LangSmith tool run stores its result as `outputs["output"]` or bare, and
unwrap to what the recorder would have seen. Extend the fixture
script with a second graph whose node calls a tool that returns
`{"status": 500, "body": "upstream down"}`.
**Acceptance:** WHEN `tool_graph.jsonl` is ingested THEN `argus check last`
SHALL fail the tool-calling node with a critical finding whose evidence
names the tool and the 500 (produced by
`inspector.inspect_tool_calls`, not a new rule).
**Verify:**
```
PYTHONPATH=src python scripts/make_langsmith_fixture.py --tool
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q -k tool
```
**Must not:** change `inspector.py` rules.

**Done 2026-09-14 (branch `s4-tool-child-runs`).** Learned: a LangSmith tool
run stores its result wrapped, `outputs = {"output": <result>}`; the recorder's
`on_tool_end` hears the bare result, so ingest unwraps it. Detection alone does
not prove the unwrap: `inspect_tool_outputs` scans nested dicts, so the wrapped
shape still fails `fetch` (as `output.status`), which is why a separate test pins
the unwrapped shape. A tool belongs to its nearest node-step ancestor, found by
walking `parent_run_id`. The tool graph lives in the fixture script, not
`demo/`. The node must pass its `config` to `tool.invoke` so the tracer
reaches the tool on Python 3.9. The verdict reads `silent_failure on fetch /
error_response on fetch_docs.status`. Proof by breaking: passing
`tool_calls=[]` fails
`test_a_swallowed_tool_500_fails_the_node_that_called_the_tool`; dropping the
unwrap fails `test_a_tool_result_is_unwrapped_to_what_the_recorder_hears`;
restored, 1024 pass.

### S-5 — Edges sidecar: `argus edges` export and `--edges` on ingest

**PR:** one.
**Depends on:** S-3.
**Files:** `src/argus/cli/cmd_edges.py` (new), `src/argus/cli/main.py`,
`src/argus/ingest/langsmith.py`, `tests/test_ingest_langsmith.py`.
**Today:** ingested runs have `graph_edge_map == {}`; a conditional source and
a terminal node are indistinguishable; `has_successors` is guessed from step
order.
**Change:** `argus edges module:factory` compiles the user's graph once and
writes `{"edge_map": {...}, "conditional_sources": [...], "node_names":
[...], "subgraph_parents": [...]}` from the 4-tuple `_topology()` returns
(import it from `recorder.py`). `argus ingest langsmith <file> --edges
edges.json` passes the first three into `grading.new_session` instead of the
step-order guess, and skips node runs named in `subgraph_parents` instead of
the S-3 descendant check.
**Acceptance:** WHEN ingest runs with `--edges` from the demo graph THEN the
saved RunRecord's `graph_edge_map` SHALL equal the recorder's for the same
graph, and the `summarize` verdict SHALL be unchanged.
**Verify:**
```
argus edges demo.fat_trace.demo_graph:build_app --out /tmp/edges.json
argus ingest langsmith tests/fixtures/langsmith/demo_graph.jsonl --edges /tmp/edges.json
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q -k edges
```
**Must not:** make `--edges` required; import `langgraph` inside `ingest/`
(the import lives in `cmd_edges.py` only).

**Done 2026-09-14 (branch `s5-edges-sidecar`).** Learned: the demo graph is a
straight line, so its real edges equal the step-order guess and the
recorder-equality test passes with or without `--edges`. It proves the
acceptance, not the wiring; a second test feeds an edge map step order can
never produce (`search` → `summarize` and `answer`) and checks it is saved.
The factory is `build_app`, not `build`, and `python -m argus.cli.main` runs
nothing (the module has no `__main__` guard), so Verify uses `argus`. A bad
edges file exits 2 and saves nothing. Proof by breaking: using the step-order
guess with `--edges` fails `test_the_edges_file_replaces_the_step_order_guess`;
ignoring `subgraph_parents` fails
`test_edges_name_the_subgraph_parents_instead_of_nesting`; restored, 1029 pass.

### S-6 — Skinny trace refuses, never passes

**PR:** one.
**Depends on:** S-3.
**Files:** `src/argus/ingest/langsmith.py`, `tests/test_ingest_langsmith.py`.
**Today:** a trace exported with LangSmith "hide outputs" has `outputs == {}`
on every run, which S-3 would read as every node returning `{}`.
**Change:** before grading, if the root run's `outputs` is absent or `{}`, or
any selected node run lacks `inputs`, or zero node runs were selected, raise
`IncompleteTraceError` saying why; exit 2 from the CLI; save nothing (BUG-1).
**Acceptance:** WHEN the demo fixture with every run's `outputs` set to `{}`
(what `hide_outputs=True` produces) is ingested THEN the CLI SHALL exit 2 and
`.argus/runs/` SHALL gain no file; WHEN only `summarize` has empty outputs THEN
ingest SHALL grade it and fail `summarize`.
**Verify:**
```
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q -k skinny
```
**Must not:** treat one node's empty `outputs` as skinny — with root outputs
present, that is the signal.

### S-7 — `--consumers` on ingest wires the contextual layer

**PR:** one.
**Depends on:** S-3.
**Files:** `src/argus/cli/cmd_ingest.py`, `tests/test_ingest_langsmith.py`,
`tests/fixtures/langsmith/drop_graph.jsonl` (new), `scripts/make_langsmith_fixture.py`.
**Today:** `contextual_findings` runs only from the recorder, with
`consumers=` passed in Python.
**Change:** `--consumers consumers.json` (`{"field": ["reader", ...]}`)
passed to `grading.finish`. Fixture: four-node graph where `enrich` nulls
`customer_id` and `respond` reads it two steps later.
**Acceptance:** WHEN `drop_graph.jsonl` is ingested with
`{"customer_id": ["respond"]}` THEN `argus check last` SHALL blame `enrich`,
not `respond`.
**Verify:**
```
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q -k consumers
```
**Must not:** infer consumers from source; that is a later step.

### S-8 — LLM rows: token accounting and truncation on both paths

**PR:** one.
**Depends on:** S-3.
**Files:** `src/argus/recorder.py`, `src/argus/ingest/langsmith.py`,
`src/argus/models.py` (`NodeEvent.llm_calls`), `tests/test_recorder.py`,
`tests/test_ingest_langsmith.py`.
**Today:** `llm_tracker` reads usage off the node's output dict, so most nodes
record zero tokens (listed as open in `docs/PIVOT-BRANCH.md`). `stop_reason`
is never seen.
**Change:** recorder adds `on_llm_end`; ingest reads descendant
`run_type == "llm"` runs. Both fill `NodeEvent.llm_calls: [{"model",
"usage", "finish_reason"}]` and sum into `RunRecord.total_tokens`. A
`finish_reason` of `length` / `max_tokens` raises a `warning` anomaly
`truncated_llm_output` on that node.
**Acceptance:** WHEN a fixture whose LLM child run has `finish_reason:
"length"` is ingested THEN the node SHALL carry a `truncated_llm_output`
finding and `total_tokens` SHALL equal the fixture's usage sum.
**Verify:**
```
PYTHONPATH=src pytest tests/test_recorder.py tests/test_ingest_langsmith.py -q -k "llm or trunc"
```
**Must not:** make truncation critical (warning only); call a real model.

### S-9 — Retired (shipped upstream)

Was: an `ignored_tool_error` rule. Upstream `9eea6c9` added
`inspector.inspect_tool_calls`: a tool that raised is critical for the node
that called it, whatever the node returned. That covers the swallowed-error
case. Nothing to build. Id kept so S-10 keeps its number. The retry case is
parked for Varad (see Parked).

### S-10 — Pin the LangGraph floor the branch actually needs

**PR:** one.
**Depends on:** nothing (can run any time).
**Files:** `pyproject.toml`, `.github/workflows/ci.yml`.
**Today:** `langgraph>=0.2.0` is declared. On 0.2.74, 56 pivot tests fail
before the recorder runs (node names equal to state keys are rejected;
`get_graph(xray=True)`, `.bound`, `langgraph_node` metadata are 0.6+).
**Change:** declare `langgraph>=0.6`; CI matrix runs the pivot suites on
`langgraph~=0.6.0` and latest `1.x`.
**Acceptance:** WHEN CI runs THEN both matrix legs SHALL pass, and a leg
pinned to 0.2.74 (run once locally, not in CI) SHALL fail at install.
**Verify:**
```
pip install -e . && python -c "import langgraph, importlib.metadata as m; print(m.version('langgraph'))"
```
**Must not:** raise the floor above 0.6; change any test.

### S-11 — Crashed node runs keep crash blame

**PR:** one.
**Depends on:** S-1, S-3.
**Files:** `scripts/make_langsmith_fixture.py`,
`tests/fixtures/langsmith/crash_graph.jsonl` (new), `tests/test_ingest_langsmith.py`.
**Today:** S-3 passes the run's `error` string as exception text, but no
fixture proves `crash_origins` still finds the origin from a file. Upstream
`9eea6c9` added nested-container blame (`state["policy"]["number"]` →
the node that wrote `{"policy": {}}`), tested only on the live recorder.
**Change:** fixture graph: `lookup` returns `{"policy": {}}`, `price` reads
`state["policy"]["number"]` and raises. Record what the tracer writes into
`error` (S-1 stub). Test ingests it and asserts the blame. If the recorded
`error` string does not contain `KeyError: 'number'` or `KeyError('number')`,
stop and log it as a bug against S-3 (mystandard §5); do not change
`inspector.py`.
**Acceptance:** WHEN `crash_graph.jsonl` is ingested THEN `argus check last`
SHALL exit 1 with `lookup` as `first_failure_step`, not `price`.
**Verify:**
```
PYTHONPATH=src python scripts/make_langsmith_fixture.py --crash
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q -k crash
```
**Must not:** change crash-blame rules; parse tracebacks inside `ingest/`
beyond passing the string through.

**Done (2026-09-15).** The recorded `error` on `price` contains
`KeyError: 'number'` (full traceback text, then LangGraph's "During task with
name 'price'" line), so S-3's pass-through was enough and no bug was logged.
Two additions beyond the spec: (1) a bystander `audit` node sits between
`lookup` and `price`. With only two nodes the fallback walk also lands on
`lookup`, so the test could not tell nested-container blame from luck.
(2) Traceback paths in `error` are rewritten to `<site-packages>` / `<repo>`,
because the raw text carried this machine's home directory. Learned: `lookup`
is also flagged by BA-006 (empty output), which alone makes it
`first_failure_step`. The first break proof (ingest drops the error string)
survived an assertion on `first_failure_step` only; the test now also requires
the `missing_field` finding naming `number` and `price`. Break proofs:
`exc = None` in ingest fails it; `nested = None` in `crash_origins` fails it
(blame moves to `audit`). Each fails only this test. Full suite 1047 passed,
6 skipped (environment: no API key, no `--argus`, no cloud pricing). No change
to `inspector.py` or `ingest/`.

## After this milestone (not now)

- Scored bench: recall / precision per rule over the fixture corpus
  (harness S-7 idea).
- Infer `consumers` from node source (`source_locator.py` + AST reads of
  `state["field"]`).
- OTel ingest once payload capture is on.
- Open-code ~100 real ingested runs before adding more rules (Hamel Husain:
  error analysis before tests).

## Deliberately does not

- Delete or bypass `ArgusWatcher` / `patcher.py`.
- Decide the three parked product questions.
- Add a new framework adapter (CrewAI etc.).
- Touch the cloud UI.
