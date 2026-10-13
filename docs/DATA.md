# What Argus sends, and what it never sends

Argus sends one small **shape card** after each graded run so we can see which
rules catch real failures and which fire on healthy runs. A shape card carries
no value your pipeline produced and no name your code chose.

> Status: the card format is final (`src/argus/data/share_card.schema.json`).
> The sender ships in 0.13.0. Until then, Argus sends nothing automatically.

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
Two people can read them: the project owner and the open-source maintainer.
Cards are deleted after 12 months.

## Paying teams

Team plans can turn telemetry on or off for the whole organization in the
team agreement. Full runs (real values) are uploaded only with
`ARGUS_CLOUD_SYNC=1`, into your own team workspace, never into this telemetry.
