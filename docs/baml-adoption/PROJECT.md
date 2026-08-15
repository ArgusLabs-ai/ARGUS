# BAML Adoption — Project Tracker

> Small living doc. Updated as we build. Not a PRD.
> Separate initiative from `docs/fix-prompt/PROJECT.md` — that feature stays
> dependency-free (deterministic template, no LLM call, confirmed in its own
> Q3). This doc is about the *other* six modules that already make LLM calls
> and hand-roll JSON parsing.

**Status:** `SCOPING` — B1 spike blocking everything else
**Owner:** Varad
**Branch:** `claude/architecture-overview-6mpow0`
**Started:** 2026-08-14

---

## 1. Why BAML is being considered

Six ARGUS modules make an LLM call expecting structured JSON back, then hand-parse
it into a frozen dataclass, wrapped in a broad `except Exception` that degrades
silently on any parse failure (consistent with ARGUS's house style — see
`docs/fix-prompt/PROJECT.md` §8 notes). BAML's value proposition (typed LLM
functions — schema-aligned parsing, retries, generated clients) is aimed exactly
at this shape of problem.

**Confirmed not to apply to `fix_prompt.py`** — that feature is a deterministic
template with no LLM call by design (offline, free, predictable across weak
models). Adding `baml-py` there would break all three properties. Not reconsidering
this without a reason to.

---

## 2. The six sites — verified inventory

| File | Function | Target dataclass | Transport |
|---|---|---|---|
| `llm_investigator.py:515` (`_parse_response`) | strips markdown fences, `json.loads` | feeds `_extract_hypotheses`, `_extract_suggested_signatures`, `_extract_suggestions` | proxy (`llm_proxy.create_chat_completion`) |
| `llm_correlator.py:190` | `json.loads(raw)` inside `augment_correlation` | `LLMCorrelationInsight` | proxy |
| `semantic_checker.py:129` | `json.loads(raw)` inside `check_semantic_coherence` | `SemanticCheckResult` | proxy |
| `heuristic_disambiguator.py:131` | `json.loads(raw)` inside `disambiguate_signals` | `DisambiguationResult` (list) | proxy |
| `source_locator.py:426` | `json.loads(content)` inside LLM fallback resolver | `dict[str, str]` (node → file:line) | proxy |
| `signature_generalizer.py:154` (`_llm_generalize`) | manual code-fence strip, regex validation, no `json.loads` (raw string, not JSON) | generalized regex string | **direct OpenAI client** via `embedding_store._get_client()` — user's own key, bypasses proxy entirely |

**Correction to initial framing:** `signature_generalizer.py` doesn't call
`llm_proxy.create_chat_completion` like the other five — it calls
`embedding_store._get_client()`, which is a direct `OpenAI()` client requiring
the user's own `OPENAI_API_KEY`. This is a pre-existing inconsistency (every
other "chat" LLM call is gated behind `argus login` / the proxy; this one is
gated behind the user owning an OpenAI key), independent of BAML. Worth fixing
regardless — see B2.

All five proxy-routed sites share the same shape: `create_chat_completion(...,
response_format={"type": "json_object"})` → check `"error" in result` → pull
`result["choices"][0]["message"]["content"]` → `json.loads` → build dataclass,
all inside `try/except Exception` that returns a safe default.

---

## 3. B1 — blocking spike (do this before scoping further)

BAML clients target a named provider (`openai`, `anthropic`, or `openai-generic`
with a custom `base_url`), expecting `/chat/completions`-shaped REST semantics
and an API-key-style auth header. ARGUS's proxy
(`{SUPABASE_URL}/functions/v1/llm-proxy`, see `llm_proxy.py` +
`supabase/functions/llm-proxy/index.ts`) is a single Supabase Edge Function
authenticated with a Supabase JWT (`Authorization: Bearer <access_token>`), whose
docstring says it "mirrors the OpenAI chat completions API shape" on success —
but returns `{"error": "..."}` on failure, not a real OpenAI error envelope, and
its URL path is not `/v1/chat/completions`.

**Unverified, not assumed:** whether BAML's generated client can point at this
endpoint at all (custom base_url + auth header are plausible, but the exact
request/response contract BAML expects hasn't been checked against what the
edge function actually sends/returns). Spike against **one** call site — proposed
pilot in §5 — before writing a migration plan for all six.

---

## 4. What must survive the migration

Every site currently guarantees it **never blocks the pipeline**:
`_skip_result(...)`, bare `return None`, `return []`, `return {}`, or an
error-carrying dataclass (`LLMCorrelationInsight(error=...)`) — always inside
broad `except Exception`. This fail-open contract has to be reproduced exactly,
not loosely approximated.

Per-call timeouts also differ and must be preserved individually, not replaced
with one generic BAML retry/timeout policy:

| Site | Timeout |
|---|---|
| `semantic_checker.py` | 5.0s |
| `heuristic_disambiguator.py` | 8.0s |
| `llm_correlator.py` | 15.0s |
| `llm_proxy.py` default (used where no override given) | 30.0s |

---

## 5. Rollout — pilot one site, not all six

**Recommended pilot: `semantic_checker.py`.** Smallest schema (`pass` / `reason`
/ `confidence`, 3 fields), hottest path (runs on every node that passes
deterministic checks, so a working integration compounds value fastest), and
smallest blast radius if something goes wrong (affects one node's status, not
correlation or investigation output shared across a whole run).

Not locked — `heuristic_disambiguator.py` is the alternative (also small,
already batches into one call per node).

---

## 6. Packaging cost

`baml-py` is a Rust-backed binary wheel plus a codegen step (`baml generate` →
a `baml_client/` directory). Before committing:

- confirm wheel availability across whatever Python versions/platforms ARGUS
  currently ships for, against `requires-python = ">=3.9"`
- decide where `.baml/` source files and the generated `baml_client/` live
  relative to the existing sdist exclusion list in `pyproject.toml`
  (`website`, `assets`, `fixtures`, `supabase`, `scripts`, `.github`)
- add a CI codegen step — same category as `scripts/build_ui.sh` for the
  Next.js dashboard, but a new kind of generated artifact to keep in sync

---

## 7. Open questions

| # | Question | Status |
|---|---|---|
| B1 | Does BAML's client work against the Supabase edge function shape, or does the edge function need a compatibility route first? | ⬜ blocking spike |
| B2 | Does `signature_generalizer.py` move onto the proxy as part of this work (fixes the pre-existing inconsistency), or stay on direct-OpenAI (smaller diff)? | ⬜ open |
| B3 | Pilot file — `semantic_checker.py` (recommended) or `heuristic_disambiguator.py`? | ⬜ open |
| B4 | After the pilot succeeds: migrate the remaining 4 proxy-routed sites in one pass, or one at a time with individual validation? | ⬜ open |

---

## 8. Decision log

| Date | Decision | Reasoning |
|---|---|---|
| 2026-08-14 | `fix_prompt.py` excluded from BAML consideration | deterministic template is a locked decision (Q3 in fix-prompt tracker); LLM call would break offline/free/predictable properties |
| 2026-08-14 | B1 spike gates the rest of this doc | six-file migration estimate is meaningless if the transport layer doesn't fit BAML's client model |
| 2026-08-14 | Treated as a separate initiative from fix-prompt, not a shared task | different risk profile (new binary dependency, packaging changes) and different blocking question (spike vs. joint wording session) |

---

## 9. Progress

| Item | Status |
|---|---|
| Six call sites inventoried and verified against source | ✅ done |
| Transport-path split identified (proxy vs. direct-OpenAI) | ✅ done |
| B1 spike | ⬜ not started — next up |
| Pilot migration | ⬜ blocked on B1 |
| Remaining sites | ⬜ blocked on pilot |
