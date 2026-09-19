# Status vocabulary

The closed set of statuses ARGUS assigns, what each means, and how node statuses roll up
into a run status. Every consumer — `argus check`, `pytest --argus`, the dashboard, exporters —
must use only the values on this page. Source of truth for the code is `argus.models.StepStatus`
and `argus.models.RunStatus` (`typing.Literal` aliases); this page and those aliases must change
together.

## Node (step) statuses — `NodeEvent.status`

| Status | Meaning | Assigned by | Counts against the run? |
|---|---|---|---|
| `pass` | Node returned output and no layer flagged it. | default | no |
| `fail` | A hard signal fired: a validator returned `False`, or the structural inspector found a critical problem (missing required field, `empty_output`, critical tool failure). | `session.py` node pipeline | **yes** — run becomes `silent_failure` |
| `crashed` | Node raised an exception. | `session.py` exception path | **yes** — run becomes `crashed` |
| `semantic_fail` | A **critical anomaly** fired on a step that was otherwise `pass` — `unreadable_update` (#111) and the behavioural `BA-*` criticals. The LLM judge does **not** assign this on its own: it only reviews soft rule flags and cannot originate a fail. File grading (`argus ingest`) never calls the judge, so ingest still reaches `semantic_fail` via those critical anomalies. Validator failures and hard rule fails cannot be overridden, so a `fail` never downgrades to `semantic_fail`. | `session.py` `on_node_end` (anomalies) | **yes** — run becomes `silent_failure` |
| `degraded_input` | Node produced syntactically valid output, but an upstream node it depends on dropped or degraded a field it consumed (`inspection.degraded_upstream_node`). The blame belongs upstream; this status marks the downstream victim. | `session.py` `_check_degraded_input` | **yes** — run becomes `silent_failure` |
| `interrupted` | Execution was cut off before the node finished (e.g. `KeyboardInterrupt`, LangGraph interrupt). | `session.py` interrupt path | **yes** — run becomes `interrupted` |
| `retried` | An earlier iteration of a node inside a loop, where the **final** iteration of that node passed. Not a failure: the pipeline self-corrected. | `session.py` `_mark_retried_iterations` at finalize | no — excluded from roll-up |
| `skipped` | A node on a conditional branch that was not taken. | `session.py` `_record_skipped` | no — excluded from roll-up |

Rules that follow from the table:

- **`retried` only exists when the final iteration is `pass`.** If the last iteration of a
  looped node fails, every iteration keeps its own status and each counts.
- **`degraded_input` never names the culprit.** Read `inspection.degraded_upstream_node` or
  `RunRecord.root_cause_chain[0]` for the origin.
- **Warnings do not change status.** Warning-severity signals (`json_in_string`, `shallow_output`,
  `truncated_llm_output`, warning-level tool failures such as HTTP 429) are recorded on the event
  but leave it `pass`. A warning-level **signature** (placeholder / refusal-like text) is the one
  soft flag the LLM judge may review — and drop, if it is a false positive. Shape warnings are
  not a reason to call the judge. A strictness knob to escalate them is planned (see `visual/PRD.md` US-1.4).
- **Critical anomaly signals do.** A critical signal on a `pass` step makes it `semantic_fail`,
  judge or no judge. `unreadable_update` is deliberately one of these: "I could not read this
  node's update" and "this node ran fine" must not be the same verdict, or a silent no-op ships
  clean (#111).

## Run statuses — `RunRecord.overall_status`

| Status | Meaning |
|---|---|
| `clean` | No active node is `crashed`, `interrupted`, `fail`, `semantic_fail`, or `degraded_input`, and no active node has a silent failure or tool failure in its inspection. This is the only value that passes `argus check` and `pytest --argus`. |
| `crashed` | At least one active node is `crashed`. |
| `interrupted` | No crash, but at least one active node is `interrupted`. |
| `silent_failure` | No crash or interrupt, but at least one active node is `fail`, `semantic_fail`, or `degraded_input`, **or** has `inspection.is_silent_failure` / `inspection.has_tool_failure`. |

"Active" = every node event whose status is not `retried` or `skipped`.

## Roll-up: node → run

Evaluated in this order; the first match wins (`session.py` `_finalize`):

```
any active node crashed          → crashed
else any active node interrupted → interrupted
else any active node in
     {fail, semantic_fail, degraded_input}
  or inspection.is_silent_failure
  or inspection.has_tool_failure → silent_failure
else                             → clean
```

Consequences worth knowing:

- A node-level `semantic_fail` becomes a run-level `silent_failure`, **not** a run-level
  `semantic_fail`. There is no run status named `semantic_fail`; the value is listed in
  `check.UNCLEAN_OVERALL_STATUSES` and `website/lib/types.ts` `RunStatus` for tolerance only and
  is never produced.
- `has_tool_failure` is `True` only for **critical** tool failures. Warning-level ones do not
  make the run `silent_failure`.
- `first_failure_step` is the first node (in execution order, including retried/skipped events)
  whose status is in `{fail, crashed, semantic_fail, degraded_input}`.

## What `argus check` / `pytest --argus` treat as failing

`argus.check.evaluate_run` fails the gate when **either**:

1. `overall_status` is not `clean`, **or**
2. any active node has status in `{fail, crashed, semantic_fail}`, or its inspection shows
   `is_silent_failure`, `has_tool_failure`, or non-empty `missing_fields`.

Exit code is `1` on failure, `0` when clean.

## The findings list — `RunRecord.findings`

Statuses say *whether* a node failed; `findings[]` says *why*, once per signal, in one flat
shape (`argus.models.Finding`): `id` (stable content hash), `node`, `type`, `severity`,
`reason` (a full sentence), `source` (`heuristic | validator | anomaly | llm | crash`),
optional `field_path`, `origin_node`, `confidence`, `suppressed`. Built at finalize by
`findings.collect_findings`; retried/skipped steps contribute nothing. Records written before
`schema_version` "2" get the list back-filled on load. Consumers should read this instead of
walking `steps[].inspection / validator_results / anomaly_signals / semantic_check`.

## Related vocabularies (do not confuse with statuses)

| Field | Values | Where |
|---|---|---|
| `Severity` | `critical` \| `warning` \| `info` \| `ok` | tool failures, anomaly signals, validator results |
| `DegradationOrigin.event_type` | `node_ok` \| `degradation_onset` \| `propagation` \| `crash` | `correlator.py` — note `crash` here is an *event type*, not a status |
| Failure types | `placeholder_detected`, `semantic_degradation`, `empty_output`, `unreadable_update`, `json_in_string`, … | `inspector.py` / `registry.py` — the *reason* a status was assigned; listed in `CLAUDE.md` |

## What a replayed run is (#79)

A rerun needs state and code. The state half is always the **ledger row** — the input
that node really saw. The code half has exactly two legal sources, and replay never
invents a third:

| Run kind | `argus replay <id> <node>` | Where the code comes from |
|---|---|---|
| Trace (`ArgusRecorder`, the pivot path) | needs `--only --app module:factory` | the caller's compiled graph |
| Trace, no `--app` given | **exit 1**, pointing at both routes | — nothing is imported |
| Legacy wrap (`ArgusWatcher`) | works as before, header says `(legacy refs)` | `node_fn_refs` the run recorded about itself |

A replayed run is a normal `RunRecord` with `parent_run_id` set, graded by the same
pipeline as any other — replay is not a status and produces no status of its own.

**Not a re-score.** Grading a saved run with no graph is `argus check <id>`, which
rebuilds the ledger and re-runs every check. `replay` means *execute again*; if there is
no code to execute, it says so instead of printing a verdict that looks like a rerun.

## Changing this vocabulary

Adding or renaming a status is a public-API change. In the same PR update: this file,
`models.StepStatus` / `models.RunStatus`, `check.py` frozensets, `findings.py` frozensets,
`website/lib/types.ts`, `CLAUDE.md` Detection Pipeline step 7, and the README colour legend.
