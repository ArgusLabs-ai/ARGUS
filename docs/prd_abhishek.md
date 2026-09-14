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
  `extra.metadata.langgraph_node` and `outputs` == update. Run fields:
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
(`create_run` / `update_run` / `batch_ingest_runs` / `multipart_ingest`,
whichever the installed `langsmith` calls — stub all four). Merge create+update
per `id`, write one JSON object per line with at least: `id`, `trace_id`,
`parent_run_id`, `name`, `run_type`, `inputs`, `outputs`, `error`, `extra`,
`start_time`, `end_time`, `dotted_order`, `tags`. No network.
**Acceptance:** WHEN the script runs THEN it SHALL write a JSONL whose `chain`
runs include one with `extra.metadata.langgraph_node == "summarize"` and
`outputs == {}`, and one root run with `parent_run_id == null`.
**Verify:**
```
PYTHONPATH=src python scripts/make_langsmith_fixture.py
python - <<'PY'
import json;rows=[json.loads(l) for l in open("tests/fixtures/langsmith/demo_graph.jsonl")]
s=[r for r in rows if (r.get("extra") or {}).get("metadata",{}).get("langgraph_node")=="summarize"]
assert s and s[0]["outputs"]=={}, s; assert any(r["parent_run_id"] is None for r in rows); print("ok", len(rows))
PY
```
**Must not:** call the network; import `langsmith.Client` for real; touch `src/`.

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
conditional_sources, reducer_kinds, *, judge, validators, strict,
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
capture_state(inputs))` then `on_node_end(node, input_snap, capture_output(
outputs) if isinstance(outputs, dict) else None, duration_ms, exc=...)`,
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
PYTHONPATH=src python -m argus.cli.main ingest langsmith tests/fixtures/langsmith/demo_graph.jsonl
PYTHONPATH=src python -m argus.cli.main check last; echo "exit=$?"   # expect exit=1, summarize
grep -nE "^(from|import) (langgraph|langchain)" src/argus/ingest/langsmith.py ; echo "expect no output above"
```
**Must not:** import the user's app; write `node_fn_refs`; upload to cloud
while logged in without `--allow-cloud`.

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
PYTHONPATH=src python -m argus.cli.main edges demo.fat_trace.demo_graph:build --out /tmp/edges.json
PYTHONPATH=src python -m argus.cli.main ingest langsmith tests/fixtures/langsmith/demo_graph.jsonl --edges /tmp/edges.json
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q -k edges
```
**Must not:** make `--edges` required; import `langgraph` inside `ingest/`
(the import lives in `cmd_edges.py` only).

### S-6 — Skinny trace refuses, never passes

**PR:** one.
**Depends on:** S-3.
**Files:** `src/argus/ingest/langsmith.py`, `tests/test_ingest_langsmith.py`.
**Today:** a run whose `outputs` key is absent (LangSmith "hide outputs"
setting, or a sampled span) would grade as a crash or as clean.
**Change:** before grading, if any selected node run lacks the `outputs` key
(absent, not `{}`) or lacks `inputs`, or if zero node runs were selected,
raise `IncompleteTraceError` with the node names; exit 2 from the CLI; save
nothing.
**Acceptance:** WHEN a fixture with `outputs` deleted from one node run is
ingested THEN the CLI SHALL exit 2 naming that node and `.argus/runs/` SHALL
gain no file.
**Verify:**
```
PYTHONPATH=src pytest tests/test_ingest_langsmith.py -q -k skinny
```
**Must not:** treat `outputs == {}` as skinny — that is the signal.

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
