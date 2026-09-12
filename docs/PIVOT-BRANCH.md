# Pivot branch — contributor update

Branch: **`pivot/fat-traces`**  
Last updated: 12 Sep 2026.

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
  tests/test_new_user_pipelines.py tests/test_inspector_unit.py -q
```

End-to-end story in `tests/test_rerun_e2e.py`: ingest → retrieve → rerank → summarize → answer. Rerank drops every doc; the graph still answers. Check blames rerank. Replay feeds that row’s input (the 3 docs) into the fixed function; old output stays `[]`, new output has docs.

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

### Cosmetic

`Finding.origin_node` is often `None`; the named origin is on `Finding.node`. Judge auto-on when a key/login exists (`semantic_judge=None`).

---

## What we changed (this branch vs `master` @ 0.11.0)

| Area | Change |
|---|---|
| Capture | `ArgusRecorder` — callbacks only; `output_update` is the node’s return |
| Notebook | `ledger.py` — rows from the run file; skipped steps omitted; reducer kinds persisted so piled-up lists survive reload |
| Contextual | Blame at the **reader**. Progressive fill is clean. Drop / never-written / written-empty still origin-blame |
| Inspector | Empty result: inherited `[]`→`[]` is warning; producer `[]` and drop-from-full stay critical |
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
| *(this push)* | Ledger-sourced replay; inherited-emptiness gate; e2e + contributor status |

---

## For contributors

- Work on **`pivot/fat-traces`**. Do not open a wrap-deletion PR into `master`.
- Do not rebuild `inspector.py` / signatures as new “layers.” They are the rules. The ledger feeds them.
- Do not stash function pointers on the recorder to make replay work.
- Full `pytest tests/` can hang on live embeddings. Use the file list above.

Untracked on purpose (not in the implementation): `docs/ARGUS-PIVOT*.pdf`, `docs/generate_pivot_*.py`, `demo/research_agent/`, `website/public/__artifact.html`.
