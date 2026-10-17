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
Two people can read them: the project owner and the open-source maintainer,
through a private panel ([`admin/`](../admin/README.md)) whose access is
enforced by row-level security in the database.
Cards are deleted after 12 months.

## Paying teams

Team plans can turn telemetry on or off for the whole organization in the
team agreement. Full runs (real values) are uploaded only with
`ARGUS_CLOUD_SYNC=1`, into your own team workspace, never into this telemetry.

## What a team workspace receives

A project that sends its runs to a team workspace sends a **run card**, not the run. The
card carries the verdict, the findings and the *shape* of the data, and never a value.
Values never leave your machine unless the project explicitly opts into full payloads.

> Status: `build_card` (`src/argus/cloud_card.py`) and its tests are in the tree; no
> release uploads a card yet.

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
- **Top-level update keys** and **each dot-separated segment of a finding's field path**
  are sent only if they look like an identifier (`^[A-Za-z_][A-Za-z0-9_]{0,63}$`, or a
  list index such as `[0]`). Anything else is sent as `?`: a dict can be keyed by the
  very value being kept out (`{"123-45-6789": …}`).
- **Keys nested inside a value** are never sent. A value is its shape only:
  `{t, len, f}`, the type, the length and features from the fixed list above. A
  boolean carries no value and a number is only "an int".

### Also never sent

- Any value from node inputs, outputs, state, tool inputs, tool outputs or tool errors
  (a tool call is its name, an HTTP status and a retry class)
- A finding's `reason`, exception messages, reviewer claims, judge and validator explanations
- Initial state, prompts and model outputs (LLM usage is call and token counts per model)
- Free-text rule names: a learned signature's category or an LLM review kind that is not a
  short snake_case word is sent as `other`

The fix prompt is the `argus fix --sanitized` one, for a failing run only: the node to
fix, the file and function to edit, field names by the rule above, shapes and exception
*types*, no values.

### How we keep it that way

`src/argus/cloud_card.py` builds every field by name, and
`src/argus/data/cloud_card.schema.json` lists every field it may carry
(`additionalProperties: false` throughout, and a pattern on every update key and field
path). `tests/test_data_leaks.py` plants a canary value in the initial state, a node
update, a nested key, a tool output, a tool error, a sentinel finding, a field path and a
judge explanation, and fails if it reaches the card or the sanitized fix prompt. It runs
on every pull request.
