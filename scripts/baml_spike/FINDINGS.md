# B1 — BAML transport-fit spike: findings

Answers the blocking question from the BAML adoption tracker §3:

> Does BAML's client work against the Supabase edge function shape, or does the
> edge function need a compatibility route first?

**Answer: it works as-is. No compatibility route is needed.** Nothing in the
edge function has to change for a BAML client to reach it, authenticate, and
return a typed result.

That unblocks the rest of the tracker. It does not by itself justify adoption —
six of the nine probes turned up caveats, and three of them change how the
migration has to be scoped. Details below.

Reproduce with:

```
pip install baml-py==0.225.0
python scripts/baml_spike/run_spike.py --verbose
```

Measured against `baml-py` 0.225.0 on Python 3.11.15, against a local replica
of the transport (`fake_proxy.py`) rather than live Supabase — the question is
about wire contracts, and a replica pins those exactly without needing a
project, a login, or a key.

---

## Results

| # | Question | Verdict |
|---|---|---|
| P1 | Can a BAML client reach the Supabase edge function at all? | ✅ PASS |
| P2 | Does BAML's auth header match what `index.ts` expects? | ✅ PASS |
| P3 | Do `max_tokens` / `temperature` / `response_format` survive to the wire? | ⚠️ CAVEAT |
| P4 | What happens to the proxy's bare `{"error": "..."}` envelope? | ⚠️ CAVEAT |
| P5 | Can a refreshed Supabase JWT be injected per call? | ✅ PASS |
| P6 | Is BAML's parser actually better than `_extract_json_object`? | ⚠️ CAVEAT |
| P7 | Can the per-call timeouts in §4 be preserved? | ⚠️ CAVEAT |
| P8 | Does a BAML `retry_policy` amplify load on the hot path? | ⚠️ CAVEAT |
| P9 | Can one BAML function cover the BYOK provider matrix? | ⚠️ CAVEAT |

---

## P1–P2: the transport fits

The tracker flagged two specific worries. Both turn out to be non-issues.

**The URL path.** BAML's `openai-generic` provider appends `/chat/completions`
to `base_url`. Setting `base_url` to the function root means the request lands
on `{SUPABASE_URL}/functions/v1/llm-proxy/chat/completions`. Supabase routes
`/functions/v1/<name>/<anything>` to `<name>`, and `index.ts` never inspects
`req.url` — it only checks `req.method !== "POST"`. So the subpath is
harmless. Observed on the wire:

```
base_url configured : http://127.0.0.1:PORT/functions/v1/llm-proxy
path BAML requested : /functions/v1/llm-proxy/chat/completions
typed result        : passed=True confidence=0.82
```

**The auth header.** BAML sends its `api_key` option as
`Authorization: Bearer <key>`. `index.ts` reads `authorization` and strips
`/^Bearer\s+/i`. The Supabase access token goes in the `api_key` slot and is
never interpreted as an OpenAI key by anything in the path. Verbatim match.

---

## P3: request parameters are dropped unless declared

By default a BAML client sends **only `model` and `messages`**:

```
default client sends : {"model": "gpt-4o-mini"}
tuned client sends   : {"max_tokens": 150, "model": "gpt-4o-mini",
                        "response_format": {"type": "json_object"}, "temperature": 0}
```

Everything ARGUS currently sets is recoverable, but only by declaring it
explicitly as a client option. Three things go missing otherwise:

- `temperature=0.0` — every judge call in ARGUS pins this. Dropping to the
  provider default makes verdicts non-deterministic.
- `max_tokens` — `semantic_checker` computes a budget per call
  (`150`, or `200 + 30n` when there are ambiguous signals). A BAML client
  option is a static number, so the dynamic budget needs a per-call
  `ClientRegistry` to survive.
- `response_format={"type": "json_object"}` — **BAML does not send this.** It
  relies on its own schema-aligned parser instead of OpenAI JSON mode. That is
  a deliberate BAML design choice, not a bug, but it means migrating a site
  silently takes the model *out* of JSON mode. See P6 for whether the parser
  covers the difference.

---

## P4: proxy errors survive, but only as strings

`index.ts` returns a bare `{"error": "<string>"}` on every failure path,
which is not OpenAI's `{"error": {"message": ..., "type": ...}}` envelope.
BAML does not choke on it — it raises `BamlClientHttpError` with
`.status_code` populated and the raw response body appended to the message:

```
HTTP 429 -> BamlClientHttpError; embedded body: {"error":"Daily limit reached (200 calls/day). ..."}
HTTP 400 -> BamlClientHttpError; embedded body: {"error":"This model does not support response_format."}
HTTP 502 -> BamlClientHttpError; embedded body: {"error":"Proxy error: connection reset"}
HTTP 401 -> BamlClientHttpError; embedded body: {"error":"Invalid or expired token. Run: argus login"}
```

4/4 recoverable. But note what that costs at the call sites. Today the message
that reaches the user comes from a dict lookup:

```python
if "error" in result:
    return _skip_result(f"check skipped: {result['error']}", model, elapsed)
```

Under BAML the same message has to be scraped back out of an exception string.
"Run: argus login" is a user-facing instruction that ARGUS surfaces on an
expired token — losing it would be a real regression, so the migration has to
carry a helper that recovers it. Worth writing once, centrally, not six times.

---

## P5: token rotation works per call

`_get_valid_credentials()` refreshes the Supabase access token, so a client
configured once at import time would pin a token that later expires.
`baml_options={"env": {...}}` overrides the value per call and reaches the
wire:

```
auth on the wire : Bearer refreshed-jwt-after-rotation
call outcome     : passed=True
```

This is the mechanism that makes BAML compatible with ARGUS's auth lifecycle.
It also happens to be the same mechanism P3 and P9 need, which is convenient:
one per-call configuration step covers token, parameters, and provider
selection together.

---

## P6: the parsing win is real, but not where the tracker aims it

The tracker's §1 pitch is schema-aligned parsing replacing hand-rolled
`json.loads`. Measured side by side:

| Model output | BAML | ARGUS today (`semantic_checker`) |
|---|---|---|
| clean JSON | parses | parses |
| ` ```json ` fenced | parses | parses (regex strip) |
| truncated mid-object | parses, `confidence` → 0.0 | parses (`_repair_truncated_json`) |
| prose, no JSON | `BamlValidationError` | returns `None` → skip |
| wrong scalar types | `BamlValidationError` | `float('high')` → except → skip |
| **missing required keys** | `BamlValidationError` | **fills defaults, returns PASS** |

Two things follow.

**The proposed pilot is the site that gains least.** `semantic_checker.py`
already hand-rolls fence-stripping *and* truncation repair
(`_extract_json_object` + `_repair_truncated_json`, ~70 lines). BAML matches
it there and beats it on type coercion — a real but narrow win. The four sites
still on bare `json.loads` (`llm_correlator`, `heuristic_disambiguator`,
`source_locator`, and the fence-stripping in `llm_investigator`) have none of
that repair logic and would gain considerably more. This is direct input to
**B3** — see below.

**One row is a live bug, independent of BAML.** On `{"reason": "ok"}` — a
response containing no verdict at all — today's code does
`bool(parsed.get("pass", True))` and returns a **passing** `SemanticCheckResult`
with `evaluated=True`. A malformed judge response is currently indistinguishable
from a genuine pass. Everywhere else in that function a parse problem routes to
`_skip_result(...)`, which sets `evaluated=False` and is correctly treated as
"no opinion". This one path skips that. It is worth fixing on its own merits
whether or not BAML is ever adopted, and it does not need a new dependency.

One caveat on the truncation row: BAML "parses" truncated JSON by filling the
cut-off field with a zero value — `confidence` came back `0.0` from a payload
where it had been truncated mid-number. Silent defaulting, not recovery. For a
field that feeds decisions that is arguably worse than failing closed.

---

## P7: per-call timeouts need a hand-rolled wrapper

This is the sharpest constraint the spike found, and it lands directly on §4's
"preserve these individually" requirement.

There is **no timeout in `BamlCallOptions`**:

```
['abort_controller', 'client', 'client_registry', 'collector',
 'env', 'on_tick', 'tags', 'tb', 'watchers']
```

`timeout_ms` and `request_timeout_ms` *are* accepted as client options, which
makes them look like the answer. They are not — BAML forwards them to the
provider as request body fields rather than enforcing them client-side. Against
a server that hangs for 3s with `timeout_ms=500` set:

```
client option timeout_ms=500 -> waited 3.00s (server hung 3.0s) then BamlClientHttpError
-> timeout_ms was forwarded to the provider as a body field: True
```

The full hang elapsed. A caller that trusted that option would block for the
server's duration, not its own.

What does work is `AbortController` driven by a caller-side timer:

```
AbortController + threading.Timer(1.0) -> BamlAbortError at 1.00s
```

Exact, and it raises a distinct exception type. So §4's 5.0s / 8.0s / 15.0s /
30.0s are preservable — as a `threading.Timer` + `AbortController` wrapper
around every call, plus `BamlAbortError` in each site's except clause. That is
strictly more machinery than the `timeout=` kwarg the sites pass today.

---

## P8: don't put a retry policy on the hot path

A `retry_policy(max_retries=2)` turns one logical call into three requests:

```
retry_policy(max_retries=2) -> 3 requests for one logical call
no retry_policy             -> 1 request for one logical call
```

BAML treats a 429 as transient. The proxy's 429 is not — it is a hard daily
quota (`DAILY_LIMIT = 200` in `index.ts`), so retrying spends quota that is
already gone and triples the latency of a call that was always going to fail.
`semantic_checker` runs on every node that passes deterministic checks, so this
is exactly the wrong place for it.

Retries are one of the three things §1 lists as BAML's value proposition. On
the proxy path specifically, the default retry behaviour is a liability rather
than a feature, and adoption should start with **no** `retry_policy` on
proxy-routed clients.

---

## P9: the migration surface is larger than §3 assumes

§3 frames B1 as a question about the Supabase proxy. That was true when the
tracker was written; it is not true of the current code. `create_chat_completion`
now tries **BYOK first** and falls back to the proxy:

```python
provider, key = _resolve_byok(api_key)
if key:
    adapter = providers.ADAPTERS[provider]     # openai | anthropic | google
    actual_model = providers.resolve_model(provider, model, ...)
    return adapter(...)
return _call_proxy(...)                        # only if no key is configured
```

So the proxy is path 2 of 2, and for any user who has run `argus key set` it is
never taken at all. A BAML migration has to cover both.

Per-call `ClientRegistry` handles the model remapping cleanly:

```
resolved openai     -> model on the wire: gpt-4o-mini
resolved anthropic  -> model on the wire: claude-3-5-haiku-latest
resolved google     -> model on the wire: gemini-2.5-flash
```

The harder part is not model names. `providers.py` does real per-provider
translation that `openai-generic` would not reproduce: `_split_system()` lifts
system messages into Anthropic's top-level `system` field and into Gemini's
`systemInstruction`; Gemini's `generateContent` needs `contents`/`parts` rather
than `messages`, `maxOutputTokens` rather than `max_tokens`, and
`responseMimeType` rather than `response_format`; and responses come back in
three different shapes that `_envelope()` normalizes. Doing this under BAML
means using its native `anthropic` and `google-ai` providers, not pointing
`openai-generic` at their URLs — which is three client configurations plus
runtime selection, not one.

**Consequence for scoping:** the tracker's §2 inventory of six call sites
undercounts the work. The unit of migration is not "six call sites" but "six
call sites × two transport paths × three BYOK providers", all currently
collapsed behind one function that returns one normalized shape. Whatever the
pilot concludes, `llm_proxy.create_chat_completion` is the seam — a BAML
adoption that does not go through it fragments provider handling that
`providers.py` currently keeps in one place.

---

## Packaging (§6) — measured

The tracker asks for wheel availability against `requires-python = ">=3.9"`.
Checked against PyPI for `baml-py` 0.225.0:

| Platform | Wheel |
|---|---|
| macOS x86_64 | `cp38-abi3-macosx_10_12_x86_64` (21.7 MB) |
| macOS arm64 | `cp38-abi3-macosx_11_0_arm64` (20.6 MB) |
| Linux x86_64 (glibc) | `cp38-abi3-manylinux_2_17_x86_64` (22.1 MB) |
| Linux aarch64 (glibc) | `cp38-abi3-manylinux_2_24_aarch64` (21.3 MB) |
| Linux x86_64 (musl) | `cp38-abi3-musllinux_1_1_x86_64` (22.3 MB) |
| Linux aarch64 (musl) | `cp38-abi3-musllinux_1_1_aarch64` (21.5 MB) |
| Windows amd64 | `cp38-abi3-win_amd64` (22.0 MB) |
| Windows arm64 | `cp38-abi3-win_arm64` (20.4 MB) |

- **Python version coverage is fine.** Every wheel is `abi3` tagged `cp38`, so
  one wheel per platform covers 3.8+ — the whole 3.9–3.12 matrix in
  `pyproject.toml` and the 3.9/3.11/3.12 CI matrix, with no per-version builds.
- **There is no sdist.** Any platform outside that table has no install path at
  all — not a slow source build, no build. Today ARGUS installs anywhere Python
  runs, because its three runtime dependencies (`typer`, `rich`, `langgraph`)
  are pure Python and every LLM call goes through stdlib `urllib`. Taking
  `baml-py` narrows the platform support matrix from "anywhere" to that list.
- **Size.** ~21 MB wheel, ~54 MB installed, against a package whose current
  runtime dependency closure carries no binary extensions at all.

On the other two §6 questions:

- **Where generated code lives.** `scripts` is already in the sdist exclude
  list and the wheel only packages `src/argus`, so this spike ships in neither
  distribution. A real adoption does not get that for free: `baml_client/`
  would have to live under `src/argus/` to be importable at runtime, which puts
  1300+ lines of generated code inside the shipped package.
- **CI codegen step.** `baml-cli generate` completes in **0.08s** and is
  bundled with `baml-py` (no separate toolchain, unlike `scripts/build_ui.sh`
  needing Node). This spike does not check in `baml_client/`; `run_spike.py`
  regenerates it on demand, which is the same shape a CI step would take.
  Cheap. Not the blocker here.

---

## Recommendations

These are inputs to the tracker's open questions, not decisions.

**B1 — resolved.** Transport fits, no edge function change needed. The
follow-up worth tracking is not "can BAML connect" but the P7 timeout wrapper
and the P9 provider surface.

**B3 — reconsider the pilot.** §5 picks `semantic_checker.py` on blast radius
and hot-path grounds, both still valid. But P6 shows it is the one site whose
parsing BAML barely improves, and P7/P8 show the hot path is where BAML's
timeout and retry gaps hurt most. A pilot there measures BAML at its
least favourable. `heuristic_disambiguator.py` (§5's stated alternative) is on
bare `json.loads`, batches one call per node, and has an 8.0s timeout rather
than 5.0s — it would show the parsing win honestly while keeping blast radius
small. **Suggest switching the pilot**, with the caveat that this trades a
better measurement for slightly more blast radius.

**B4 — one at a time, and the first one is not a BAML change.** P4, P7, and P9
each need a shared helper (error-string recovery, timer+abort wrapper, per-call
client construction). All three belong at the `llm_proxy` seam. Building them
during the pilot and reusing them is what makes a later one-pass migration
plausible; building them six times is what makes it not.

**Independent of BAML.** Two findings here need no dependency and should not
wait on this initiative:

1. The `{"reason": "ok"}` → silent PASS path in `semantic_checker` (P6).
2. `signature_generalizer.py`'s direct-OpenAI client (tracker B2) — unchanged
   by anything measured here, still worth fixing on its own.
