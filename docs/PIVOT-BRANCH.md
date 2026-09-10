# Pivot branch status — `pivot/fat-traces`

Last updated: 10 Sep 2026.

This branch is the **implementation pivot**: same product (silent failures, origin blame, `argus check`), new capture (fat traces, no engine wrap). Strategy briefs live in `docs/ARGUS-PIVOT.pdf` and `docs/ARGUS-PIVOT-CONTRIBUTORS.pdf` (local; not required on the branch).

**Push this branch. Do not merge wrap deletion into `master` in the first PR.** The first PR into `master`, when we open it, is additive: `ArgusRecorder` sits next to `ArgusWatcher`. `patcher.py` / `watcher.py` stay until pytest `--argus` and replay have a recorder path.

---

## What is implemented

The pipe:

```
ArgusRecorder.attach(app)          # callbacks; no patch_graph, no rebound invoke
  → fat trace (node, input, update, tools, errors)
  → ledger (notebook from the saved run file)
  → three rule checks together:
       contextual     declared consumers={"field": ["reader"]}
       inspector      empty_output, tool empty/error, HTTP-in-payload
       signatures     placeholders, refusals, garbage  (not a model)
  → LLM judge last (opt-in, semantic_judge=False by default)
       cannot override a critical from the rules
  → verdict = argus check / evaluate_run
       investigate() essay is not the verdict
```

User API:

```python
from argus import ArgusRecorder

app = ArgusRecorder(consumers={"b": ["D"]}).attach(app)
app.invoke({"query": "..."})
# then: argus check
```

| Layer | What landed | Proof |
|---|---|---|
| **1. Ingest** | `src/argus/recorder.py` — LangChain callbacks, `output_dict` = the node's return, not merged state. Thin/empty traces raise `IncompleteTraceError`. | `tests/test_recorder.py`, `demo/fat_trace/demo_graph.py` |
| **2. Ledger** | `src/argus/ledger.py` — one row per step: input, update, `state_after`, tools, error. Tools persist on `NodeEvent.tool_calls`. Re-score = `load_run` + `evaluate_run`, no second invoke. | `tests/test_ledger.py` |
| **3. Contextual** | `src/argus/contextual.py` — declared consumer map. Blame the first row whose running state lacks the field (never written → A; dropped → the dropper). Not N vs N+1. Wired via `_blame_origins` before `finalize()`. | `tests/test_contextual.py` |
| **Structure / tools / semantic rules** | Existing `inspector.py` + `signatures.json`. Not rewritten. Still run in `on_node_end`. | Fable audit + refusal/empty_output tests |
| **4. Judge last** | Existing `semantic_checker.py`. Default off. Stubbed pass cannot wash out `{}` or a contextual miss. | `tests/test_judge_last.py` |
| **Reducers (grading)** | `attach()` reads `extract_reducer_fields(app.builder)` into `session.reducer_fields`. Fan-in `Annotated[list, operator.add]` is graded with the real reducer, not a plain overlay. | `test_reducers_are_read_off_the_graph` |
| **Essay hole** | `semantic_judge=True` no longer also runs `investigate()` at finalize. Per-step judge still fires; `enabled` is cleared before `finalize()`. | recorder `_finish` |
| **Replay on trace runs** | `argus replay` on an `ArgusRecorder` run exits **1** and points at `argus check <id>`. It must not print `--app` and exit 0. | `test_replay_refuses_a_trace_run_loudly` |
| **pytest uninstall leak** | `install → uninstall → install` restores Pregel methods (`__wrapped__` recovery + `pytest_unconfigure`). Wrap path still used by `--argus`. | `tests/test_pytest_plugin.py` |

Demo:

```bash
PYTHONPATH=src python demo/fat_trace/demo_graph.py   # graph "succeeds"
argus check                                          # exit 1 — silent_failure on summarize / empty_output
```

Pivot tests (do not run the full suite; network tests hang):

```bash
PYTHONPATH=src pytest tests/test_recorder.py tests/test_ledger.py \
  tests/test_contextual.py tests/test_judge_last.py -q
```

---

## What is still the old path

These still go through `ArgusWatcher` / `patcher.py`. That is intentional until the issues below land.

| Surface | Today |
|---|---|
| `pytest --argus` and CI eat-own-cooking (`tests/test_argus_ci_gate.py`, #68) | Patches `StateGraph.compile` + Pregel `invoke` / `ainvoke` / `stream` / `astream` / `batch` / `abatch` |
| `argus replay <id> <node>` (live re-execution) | Needs `node_fn_refs` / `app_factory_ref` / HTTP cassettes from the watcher |
| `record_http` / urllib3 cassettes | Watcher only. No fake HTTP column on the ledger |
| `ArgusWatcher` public API | Unchanged |

---

## Known gaps (not blockers for pushing this branch)

### 1. Ledger `state_after` vs reducers (medium)

Grading uses real reducers (`session.reducer_fields`). The **notebook** still overlays dicts. For `docs: Annotated[list, operator.add]`, a node returning `{"docs": []}` leaves `state_after["docs"] == []` while LangGraph kept the accumulated list.

Not fixed live-only on purpose: reducers are callables and do not survive the run file. A live-only fix would make the reloaded notebook differ from the live one and break `test_ledger_from_live_steps_matches_the_reloaded_one`.

Fix later: persist reducer identity, or rebuild `state_after` from each successor's recorded `input_state` (needs the edge map for fan-out). See the ponytail comment in `build_ledger()`.

### 2. pytest `--argus` on the recorder (large — blocks deleting wrap)

Plugin still patches compile + six Pregel methods. Candidate: `langchain_core.tracers.context.register_configure_hook` (present in langchain-core 0.3.86) so the recorder is process-wide with no class patching. Open question: a global hook has no `app` for `get_graph()`, so node names / edges / reducers must be derived lazily from the callback stream.

**Done when:** `pytest tests/test_argus_ci_gate.py --argus` passes with `patch_graph` monkeypatched to raise.

### 3. What `argus replay` means (medium)

Trace runs now fail loudly and point at `argus check`. Choose later:

- **(a)** replay = re-score from the file for recorder runs; keep live re-execution only for legacy watcher runs, or
- **(b)** drop live re-execution and delete `replay.py`'s `node_fn_refs` path.

The brief: re-importing live functions is not the default.

### 4. HTTP I/O on the ledger (deferred)

Tool callbacks are the I/O we have. No urllib3 monkeypatch on this path. No empty `http=[]` column.

### 5. Other frameworks / skinny traces that look fat (later)

`ArgusRecorder` is LangGraph-specific (`app.get_graph()`, `metadata["langgraph_node"]`). CrewAI (or anything else) is out of scope until that runtime exposes each step's **return value**, not merged state. `IncompleteTraceError` catches missing steps, not “`output_dict` is actually merged state.”

### 6. Delete wrap (`patcher.py`, `watcher.py`, `http_recorder.py`)

**Blocked on 2 and 3.** Do not delete on this branch's first merge to `master`. CI `#68` still depends on the wrap.

### Cosmetic

Contextual blame is on `Finding.node`. `origin_node` stays `None` because `collect_findings` derives it from `degraded_upstream_node`. The named origin is correct.

---

## Commits on this branch (from `master` @ 0.11.0)

| Commit | What |
|---|---|
| `6bafc9d` | Pivot notes in `CLAUDE.md` |
| `6c61a9a` | Fat-trace recorder, demo |
| `af25d3e` | Ledger, contextual, judge-last tests |
| `34ad325` | pytest `--argus` uninstall restores Pregel methods |
| `8703887` | Reducers on recorder path; skip investigation essay |
| `00aefdb` | `argus replay` fails loudly on a trace run |

---

## Push / PR

- **Now:** push `pivot/fat-traces` to origin. Do not push to `master`.
- **Later PR into `master`:** this code, wrap path left in place. Title it as a new capture path, not a replacement of the core.
- **Do not include in that PR:** deletion of `patcher.py` / `watcher.py`, a fake HTTP ledger column, CrewAI, inferred consumer maps.

Untracked on purpose (not part of the implementation): `docs/ARGUS-PIVOT*.pdf`, `docs/generate_pivot_*.py`, `demo/research_agent/`, `website/public/__artifact.html`.
