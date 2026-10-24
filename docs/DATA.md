# What Argus sends, and what it never sends

Argus sends one small **shape card** after each graded run so we can see which
rules catch real failures and which fire on healthy runs. A shape card carries
no value your pipeline produced and no name your code chose.

> Status: built and live from the release that ships `src/argus/telemetry/`
> (0.13.0). Earlier versions send nothing automatically.

## Turn it off

Any one of these stops all telemetry:

| How | Scope |
|---|---|
| `argus telemetry off` | This machine, saved in `~/.argus/telemetry.json` |
| `ARGUS_TELEMETRY=0` | The current process or CI job |
| `DO_NOT_TRACK=1` | Every tool that honours the [convention](https://consoledonottrack.com) |

`argus telemetry status` prints whether it is on and where the setting came
from. The first time a run would send a card, Argus prints a one-line notice.

## What a card contains

| Field | Example | What it is |
|---|---|---|
| `install` | `i_7f3a91c2…` | A random ID made on your machine. Delete `~/.argus/telemetry.json` to get a new one |
| `run` | `r_0b4e…` | A keyed hash of the run ID, so follow-up events can refer to it |
| `argus_version`, `langgraph_version`, `python` | `0.13.0`, `1.0.3`, `3.12` | Versions |
| `ci` | `true` | Whether the run happened in CI |
| `fingerprint` | `g_91c2…` | A keyed hash of the graph's shape, so 500 CI runs of one pipeline count once |
| `status`, `blamed` | `fail`, `[k_7f3a…]` | The verdict and the hashed node(s) blamed |
| `graph` | `{nodes: 6, edges: 7, cyclic: false}` | Counts only |
| `steps[]` | see below | Per node: hashed name, status, attempt, duration, the **shape** of its update and tool outputs, the reviewer's role and kind |
| `findings[]` | `{type: "status_overstated", severity: "critical", heuristic: true, node: k_…}` | Each rule that fired |

### Names become keyed hashes

`customer`, `refund_node`, `create_refund` become `k_91c2…`, an HMAC with a
secret stored in `~/.argus/telemetry.json` that is **never sent**. The same
name hashes the same way across your runs, so we can follow one node over
time, but nobody who sees the card can test a guess like "is this `customer`?".

### Values become shapes

Your node returns:

```json
{"customer": {"email": "ana@acme.com"}, "refund_status": "scheduled"}
```

The card says:

```json
{"t": "object", "keys": {
  "k_91c2…": {"t": "object", "keys": {"k_4be0…": {"t": "string", "len": 12, "f": ["email_like"]}}},
  "k_e11d…": {"t": "string", "len": 9, "f": ["done_word"]}}}
```

A string is its length plus features from a fixed list, all computed on your
machine: `empty`, `multiline`, `json_like`, `url`, `email_like`, `numeric`,
`done_word`, `pending_word`, `error_word`, `refusal`, `placeholder`. A boolean
keeps its value (`ok: false` is the signal). Numbers keep only their type.

### Never sent

- Any value from node inputs, outputs, state or tool calls
- Node, state-key, tool or field names (only keyed hashes)
- Exception messages, finding reasons, reviewer claims or corrections
- Prompts, model outputs, API keys, file paths, hostnames, usernames, email
- Free-text rule names: a learned signature's category or an LLM review kind
  that is not a short snake_case word is sent as `other`

## How we keep it that way

1. **Allowlist, not redaction.** `src/argus/telemetry/card.py` builds every
   field by name. There is no code path that copies part of the run.
2. **Published schema.** `share_card.schema.json` lists every field and every
   allowed feature word. The server rejects a card with any field it does not
   list, so a buggy future client cannot send more.
3. **Canary tests.** `tests/test_telemetry_card.py` fills a pipeline with fake
   secrets, in values, names, exceptions and reviewer claims, and fails if any
   reaches a card. It runs on every pull request.
4. **You can read every card.** Each card is written to `.argus/outbox/`
   before it is sent.

## Where it goes and who sees it

Cards go over HTTPS to an Argus-run Supabase project used for nothing else.
Only the project owner and the maintainers the owner adds can read them,
through a private admin panel, and each maintainer sees only what the owner
grants: the overview, the rule scoreboard, the cards themselves.
Access is enforced by row-level security in the database, not by the panel.
The tables, policies and permissions are public:
[`supabase/telemetry/migration.sql`](../supabase/telemetry/migration.sql) and
[`supabase/telemetry/permissions.sql`](../supabase/telemetry/permissions.sql).
Cards are deleted after 12 months.

## Paying teams

Team plans can turn telemetry on or off for the whole organization in the
team agreement. Full runs (real values) are uploaded only with
`ARGUS_CLOUD_SYNC=1`, into your own team workspace, never into this telemetry.
A run whose card is uploaded to a team workspace is not also sent as telemetry:
[how the action ensures it](#a-run-sent-to-a-workspace-is-not-also-telemetry).

## What a team workspace receives

A project that sends its runs to a team workspace sends a **run card**, not the run. The
card carries the verdict, the findings and the *shape* of the data, and never a value.
Values never leave your machine unless the project explicitly opts into full payloads.

> Status: `build_card` (`src/argus/cloud_card.py`), `argus cloud upload` and their tests are
> in the tree; no release uploads a card yet.

A card, abridged (all values here are synthetic):

```json
{
  "schema": 1,
  "run": {
    "run_id": "20261017-101010-a1b2c3", "argus_version": "0.15.0",
    "overall_status": "silent_failure", "complete": true, "end_reason": "finished",
    "duration_ms": 840.5, "started_at": "2026-10-17T10:10:10.123456+00:00",
    "agent": "refund-agent", "environment": "ci", "test_id": null
  },
  "topology": {"graph_node_names": ["lookup", "reply"], "graph_edge_map": {"lookup": ["reply"], "reply": []}},
  "steps": [{
    "node": "lookup", "status": "fail", "superstep": "#1", "duration_ms": 312.4,
    "update": {"t": "object", "len": 2, "keys": {
      "account": {"t": "object", "len": 3},
      "refund_status": {"t": "string", "len": 9, "f": ["done_word"]}}},
    "tools": [{"name": "crm_lookup", "status": 503, "failure_class": "transient", "error": false}]
  }],
  "findings": [{
    "id": "d17fb5a321", "type": "status_overstated", "node": "lookup",
    "field_path": "refund_status", "origin_node": null, "severity": "critical",
    "source": "heuristic", "confidence": null, "failure_class": "fatal", "suppressed": false
  }],
  "llm_usage": [{"model": "gpt-4o", "calls": 2, "prompt_tokens": 1300, "completion_tokens": 230, "total_tokens": 1530}],
  "root_cause_chain": ["lookup"],
  "fix_prompt": "# Fix: `lookup` returns a result even though …"
}
```

### The naming rule

Unlike the shape card above, a run card keeps names readable, so the rule is about what a
name may be:

- **Node and tool names** are chosen in your code and are sent as written. So are the **agent**
  and **environment** of a run: names you set (`ArgusRecorder(agent=)`, `ARGUS_AGENT`,
  `ARGUS_ENVIRONMENT`), else the compiled graph's name, else a hash of the node names;
  `ci` on GitHub Actions and `local` otherwise.
- **Top-level update keys** are sent only if they look like an identifier
  (`^[A-Za-z_][A-Za-z0-9_]{0,63}$`). Anything else is sent as `?`: a dict can be keyed by the
  very value being kept out (`{"123-45-6789": …}`).
- **Each dot-separated segment of a finding's field path** is sent only if it looks like an
  identifier (or is a list index such as `[0]`) **and** is a real key, or an index in range, at
  its level in that node's update, input state or tool output. A path is written with dots and
  a key can contain one (`{"john.smith": …}`), so the first segment that is not a real key,
  and every segment after it, is sent as `?`: `accounts.john.smith.balance` is
  `accounts.?.?.?`. A path of one segment is a field name, not a place inside a value, and is
  sent when it looks like an identifier.
- **Keys nested inside a value** are never sent. A value is its shape only:
  `{t, len, f}`, the type, the length and features from the fixed list above. A
  boolean carries no value and a number is only "an int".

### A green run on the default branch: the baseline part

When a job is a **push to the repository's default branch** and the run is clean, its card
also carries `baseline_part`: what `argus baseline` would learn from that run, per node. That
is the keys the node wrote, their types, a kind tag per field path (`text`, `nonneg`,
`object`) and the min / max length of each list or string. No value is in it.

This is the one place a card names keys nested inside a value (`customer.tier`,
`docs[0].text`), because a baseline that cannot name a field cannot check it. The rule is
stricter than the one above: a key or path is sent only if **every** segment looks like an
identifier and is verified against that node's own updates; anything else (a value used
as a key, a key that contains a dot) is left out of the part entirely, not sent as `?`.
Leaving a field out only makes the baseline claim less. Pull requests, other branches and
red runs send no part. The workspace stores a part only when the job's token says it ran
on the default branch.

### Also never sent

- Any value from node inputs, outputs, state, tool inputs, tool outputs or tool errors
  (a tool call is its name, an HTTP status and a retry class)
- A finding's `reason`, exception messages, reviewer claims, judge and validator explanations
- Initial state, prompts and model outputs (LLM usage is call and token counts per model)
- Free-text rule names: a learned signature's category or an LLM review kind that is not a
  short snake_case word is sent as `other`
- The description of a signature your project added or took from the community: an LLM
  writes it from real outputs, so the fix prompt says that such a signature matched and leaves
  its text out. The descriptions of the signatures Argus ships stay.

The fix prompt is the `argus fix --sanitized` one, for a failing run only: the node to
fix, the file and function to edit, field names by the rule above, shapes and exception
*types*, no values.

### How we keep it that way

`src/argus/cloud_card.py` builds every field by name, and
`src/argus/data/cloud_card.schema.json` lists every field it may carry
(`additionalProperties: false` throughout, and a pattern on every update key and field
path). `tests/test_data_leaks.py` plants a canary value in the initial state, a node
update, a nested key, a key that contains a dot, a tool output, a tool error, a sentinel
finding, a field path, a judge explanation and the description of a learned signature, and
fails if it reaches the card, a baseline part or the sanitized fix prompt. It runs on every pull request.

### What a job takes back: baselines

Before the tests, cloud mode runs `argus cloud pull-baselines`: a `GET` to
`$ARGUS_CLOUD_URL/api/v1/baselines` with the same OIDC token and no body. The answer is the
baseline parts of the repository's last green main runs, per agent. They are merged on the
runner by the open-source code (`trace_rules.merge_baselines`) and written to
`.argus/baselines/<agent>.json`, which the recorder reads. Nothing is sent but the token, and
nothing comes back but key names, types, kind tags and length ranges your own main runs sent.

### How a job sends it

`argus cloud upload [--final]` posts `{"schema": 1, "final": <bool>, "cards": [...]}` to
`$ARGUS_CLOUD_URL/api/v1/runs`: one card for each run the pytest session saved
(`pytest --argus-report-only` lists them in `.argus/session_runs.txt`). The job proves who it is
with the OIDC token GitHub Actions issues for it (audience `argus-cloud`; the job needs
`permissions: id-token: write`), so there is no secret to store and the body names no
repository, ref or commit: the workspace reads those from the token.

The command exits 1 with one line on anything but a 2xx answer, on a timeout, and when the
token, the list of runs or a run is missing, so a job that could not report never looks like
one that did. It never prints a card, a response body or the token, follows no redirect (the
token is not passed on to another host), sends the token only to an `https` URL, and uses
nothing beyond the Python standard library. `tests/test_data_leaks.py` checks that the body it
sends is exactly `build_card`'s output for every canary above.

### A run sent to a workspace is not also telemetry

A run whose card goes to a team workspace is not also sent as the anonymous shape card
described at the top of this page. The gate action ensures it: with `cloud: "true"` it exports
`ARGUS_TELEMETRY=0` before it runs your tests, so nothing those tests record can be sent as
telemetry, and the card goes only to the workspace. If you run `pytest --argus-report-only` and
`argus cloud upload` yourself, set `ARGUS_TELEMETRY=0` (or `DO_NOT_TRACK=1`) for the test
step to get the same result.

The opt-in full-run sync (`ARGUS_CLOUD_SYNC=1`) is off whenever a run is recorded in
report-only mode (`--argus-report-only`, `ARGUS_REPORT_ONLY=1`) or `ARGUS_CLOUD_URL` is set:
a workspace receives run cards, never the run.
