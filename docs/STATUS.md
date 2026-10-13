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
| `retried` | An earlier iteration of a node inside a loop, where the **final** iteration of that node passed. Not a failure: the pipeline self-corrected. Parallel `Send` workers of one superstep are one iteration, not several. | `session.py` `_apply_loop_retries` at finalize | no — excluded from roll-up |
| `skipped` | A node on a conditional branch that was not taken. | `session.py` `_record_skipped` | no — excluded from roll-up |

Rules that follow from the table:

- **`retried` only exists when the final iteration is `pass`.** If the last iteration of a
  looped node fails, every iteration keeps its own status and each counts. The final
  iteration is a whole superstep: if any `Send` sibling in it failed, nothing is relabelled,
  and siblings never relabel each other.
- **`degraded_input` never names the culprit.** Read `inspection.degraded_upstream_node` or
  `RunRecord.root_cause_chain[0]` for the origin.
- **Warnings do not change status by default.** Warning-severity signals (`json_in_string`, `shallow_output`,
  `truncated_llm_output`, warning-level tool failures such as HTTP 429, and a node's own verdict
  shapes under `own_output=True` — status words / `errors: [...]` lists) are recorded on the event
  but leave it `pass`. `inspection.has_tool_warnings` is `True` when any warning-severity tool
  failure was recorded; escalate them at check time with `argus check --strict warn_as_fail`
  (PRD US-1.4 / #73). Any warning-level entry in `inspection.semantic_signals` is a soft flag the
  LLM judge may review — and drop, if it is a false positive (shape warnings included; F-21).
  **Exception — the run reviewer** (`argus.review`, on when node purposes are given): a warning
  on a step the reviewer independently verified becomes a critical `review_confirmed` tool
  failure, and the step `fail`. A step with no rule signal at all fails (`review_verified`)
  only when two different models verified the same item.
- **Heuristic criticals need the run reviewer when it runs.** D4 / D6 / D12–D15, an HTTP 404
  body and the contextual "never written" guess leave the step `pass` (kept as warnings) unless
  the reviewer verified that step. Strict criticals are unaffected; with no reviewer, nothing
  here changes.
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
- `has_tool_failure` is `True` only for **critical** tool failures. Warning-level ones set
  `has_tool_warnings` instead and do not make the run `silent_failure` under the default
  roll-up. Use `argus check --strict warn_as_fail` to fail CI on those warnings.
- `first_failure_step` is the first node (in execution order, including retried/skipped events)
  whose status is in `{fail, crashed, semantic_fail, degraded_input}`.

## What `argus check` / `pytest --argus` treat as failing

`argus.check.evaluate_run` fails the gate when **either**:

1. `overall_status` is not `clean`, **or**
2. any active node has status in `{fail, crashed, semantic_fail}`, or its inspection shows
   `is_silent_failure`, `has_tool_failure`, or non-empty `missing_fields`.

With `strict="warn_as_fail"` (`argus check --strict warn_as_fail`), warning-severity tool
failures (`has_tool_warnings` / rate limits, etc.) also fail the gate even when
`overall_status` is still `clean`. Default is `critical_only`.

Exit code is `1` on failure, `0` when clean.

### Which error text is a non-gating `rate_limit` warning (intended behavior change, PRD-02)

An `error` field becomes a `rate_limit` **warning** (never gates) only when `failure_class.is_rate_limit_text`
reads it as one: an HTTP 429 in context (`HTTP 429`, `status code: 429`, `429 Too Many Requests`,
`ResourceExhausted: 429 ...`, `got a 429`, `Request failed (429)`) or the words *rate limit*, *too many
requests*, *throttled*. The old matcher was a bare substring search (`rate.?limit|quota.?exceed|too.?many.?requests?|429`),
so these verdicts changed on purpose (checked with a 169-string differential against the pre-PRD-02 inspector):

| Error text | Before | Now | Why |
|---|---|---|---|
| `quota exceeded` (any `quota.?exceed`) | `rate_limit` warning | critical `error_response` | a billing / plan limit, not a rate: waiting does not help |
| `429` inside another number or as an id, count or line: `Order 1429 not found`, `port 4290`, `Item 429 is out of stock`, `Line 429`, `KeyError: 429` | `rate_limit` warning | critical `error_response` | not an HTTP status |
| a leading 4xx/5xx followed by a unit or count noun: `429 MB file limit exceeded`, `500 items required` | `rate_limit` warning (429) / read as a status (5xx) | critical `error_response`, class `fatal` | a quantity, not a status |
| a leading 5xx followed by a bare time unit: `503 seconds`, `500 s elapsed` | critical `error_response` (no class) | critical `error_response`, class `fatal` | a duration, not a status, so no longer read as `transient` (retry once) |
| a leading 429 followed by a bare time unit: `429 seconds`, `429 secs`, `429 s left` | `rate_limit` warning | `rate_limit` warning (unchanged) | after a 429 the unit is a retry hint, so it still reads as a 429 rate limit |
| `rate` + `limit` inside a longer word: `accurate limit exceeded`, `the corporate limit is 5` | `rate_limit` warning | critical `error_response` | not about rates |
| `too many request parameters supplied`, the singular `Too Many Request` | `rate_limit` warning | critical `error_response` | bad input, not a rate limit |
| `throttled`, `ThrottlingException: Rate exceeded` | critical `error_response` | `rate_limit` warning | a throttle is a rate limit (the one change that makes a run more lenient) |

Real rate limits are unchanged when the 429 reads as a status (`HTTP 429`, `got a 429 from the API`,
`Request failed (429)`, `hit a 429 rate limit`) and is followed by an HTTP reason phrase (`429 Too Many Requests`),
`from`, `rate limit`, `limit` / `limits` (`hit a 429 limit exceeded`, `got 429 limits`), a connective or
preposition on the stoplist in `failure_class.py` (`after`, `during`, `despite`, `via`...: `got 429 during
retries`), end of text or punctuation. The words themselves (`rate limit exceeded`, `Too Many Requests`,
`ratelimited`) are unchanged too. What did change is the plural-noun and count cases, below.

#### A number followed by a plural noun after a call verb is read as a count, including `errors` and `responses`; the failure still gates

After a call verb (`got`, `received`, `returned`, `hit a`, `failed (`) a number followed by a plural noun
(optionally one word first, as in `new messages`), `id`, or, for a 5xx, `limit`, is a count, not a status: `got 429 messages in
queue`, `failed (429) records skipped`, `got 500 new messages`, `hit a 500 limit`, and so
`Request failed (503) Services unavailable`. This is **deliberate for `errors` and `responses` too**: ambiguous
wording must fail closed, because reading a count as a 429 would turn a critical failure into a non-gating
warning, while reading a real status as a count leaves it critical (only the retry class, or the rate-limit
warning, is lost). Every row below is still a gating `error_response`.

| Error text | Before | Now | Why |
|---|---|---|---|
| `got 429 responses`, `got 429 responses from the API` | `rate_limit` warning | critical `error_response`, class `fatal` | a plural noun after a call verb is a count |
| `API returned 500 errors`, `API returned 500 errors from upstream`, `got 503 errors`, `received 500 responses` | critical `error_response` (no class) | critical `error_response`, class `fatal` (a 5xx status would be `transient`) | same: `errors` and `responses` are plural nouns like any other |

Words that only end in `s` are not plurals (`got 503 news`, `lens`, `always`, `series`, `this`, `status`,
`access`), and the one word skipped before a plural is never a preposition or connective
(`got 503 during deploys`), so those stay statuses. Pinned by `tests/test_failure_class.py`
(`test_the_deliberate_rate_limit_verdict_changes_are_pinned`, `test_a_count_after_a_call_verb_is_not_a_status`,
`test_a_plural_after_a_call_verb_is_a_count_even_for_responses_and_errors`,
`test_a_429_limit_after_a_call_verb_is_a_rate_limit_not_a_count`,
`test_a_preposition_or_connective_is_not_a_skipped_adjective`,
`test_a_word_that_only_looks_like_a_plural_is_not_a_count`,
`test_a_status_after_a_call_verb_is_still_a_status`).

## The findings list — `RunRecord.findings`

Statuses say *whether* a node failed; `findings[]` says *why*, once per signal, in one flat
shape (`argus.models.Finding`): `id` (stable content hash), `node`, `type`, `severity`,
`reason` (a full sentence), `source` (`heuristic | validator | anomaly | llm | crash`),
optional `field_path`, `origin_node`, `confidence`, `suppressed`, `failure_class`
(`fatal | transient | rate_limited` on tool-failure findings, else null). Built at finalize by
`findings.collect_findings`; retried/skipped steps contribute nothing (they only feed
the info finding `recovered_after_transient`). Records written before
`schema_version` "2" get the list back-filled on load. `argus claims` later adds
`broken_claim` warnings (`origin_node` is the node that made the claim, `field_path` the
claim id) and, on a re-run, replaces only those. Consumers should read this instead of
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
