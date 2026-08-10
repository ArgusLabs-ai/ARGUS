# ARGUS Open-Core Separation + BYOK — Design Spec

**Ticket:** VAR-93
**Date:** 2026-08-11
**Status:** Approved for planning

## Objective

Split ARGUS into a free open-source core (`src/argus/`, the PyPI package) and a
proprietary hosted/enterprise layer (`cloud/`), in the **same public repo, same
branch**. A `pip install argus-agents` must give a fully working, bring-your-own-key
(BYOK) product that never touches ARGUS-owned infrastructure. The hosted/enterprise
deployment keeps working unchanged by setting environment variables.

No forks. No enterprise branch. The open/enterprise boundary is a **folder**, not a
git boundary.

## Confirmed decisions

1. **BYOK is OpenAI-only** for v1. `ANTHROPIC_API_KEY` support is deferred (all
   internal LLM calls already use OpenAI-shape models).
2. **Cloud sync is enterprise-only.** OSS users run local-only (`.argus/runs/*.json`).
   No BYO-Supabase for OSS users; the storage env-var hooks exist but are undocumented.
3. **No plugin/interface layer.** The core already degrades gracefully (cloud calls
   are lazy and no-op when unauthenticated). We rely on env-var config + existing
   no-op degradation, not `cloud/auth.py`/`cloud/sync.py` abstractions.
4. **Local BYOK key persistence.** A CLI flow lets the user save their OpenAI key to
   `~/.argus/config.json` (chmod 600) so it is reused across sessions without
   re-entry.
5. **Repo stays public** (already is). Dual-license to make open-core legally coherent.

## Current state (why this is needed)

- `llm_proxy.py` forces all LLM calls through the ARGUS Supabase Edge Function proxy;
  `_has_own_key()` is hardcoded `False`. An outside user cannot run LLM features.
- `cloud.py:18-24` hardcodes the ARGUS Supabase URL + anon key.
- The proxy's real secret (OpenAI key) already lives in Supabase (`Deno.env`), **not**
  in the repo — confirmed safe.
- `LICENSE` currently reads "All rights reserved" — proprietary, which contradicts
  open source and blocks external use/contribution.

## Key resolution order (LLM)

```
1. OPENAI_API_KEY env var            → call OpenAI directly (BYOK, 12-factor override)
2. saved key in ~/.argus/config.json → call OpenAI directly (BYOK, persisted)
3. logged in AND hosted configured   → route through ARGUS proxy (existing behavior)
4. none of the above                 → return {"error": ...}; callers fall back to
                                        heuristic-only detection (no crash)
```

All 11 current callers of `create_chat_completion` already check for an `"error"` key
and degrade, so callers are unchanged.

## Architecture: after

```
ARGUS/
├── src/argus/          ← open-source core (Apache-2.0) — the PyPI package
│   ├── llm_proxy.py    ← BYOK path + proxy fallback
│   ├── user_config.py  ← NEW: load/save local BYOK key (~/.argus/config.json)
│   ├── cloud.py        ← Supabase URL/key from env, no-op when unset
│   └── cli/cmd_key.py  ← NEW: argus key set/show/clear
├── cloud/              ← proprietary (excluded from PyPI)
│   ├── config.py       ← the real Supabase URL, anon key, proxy URL (public-safe)
│   └── LICENSE         ← proprietary license
├── supabase/           ← migrations + llm-proxy edge function (already build-excluded)
├── website/            ← hosted dashboard + billing (proprietary)
└── LICENSE             ← Apache-2.0 + note pointing to cloud/LICENSE
```

## Work breakdown

### 1. LLM layer — BYOK (`llm_proxy.py`, `user_config.py`, `cmd_key.py`, `embedding_store.py`)

- **`src/argus/user_config.py`** (new, small): `get_openai_key()` /
  `set_openai_key(key)` / `clear_openai_key()`, reading/writing
  `~/.argus/config.json` (chmod 600). Reuses the `~/.argus/` dir pattern already used
  by `cloud.py` credentials.
- **`llm_proxy.py`**: implement the 4-step resolution order above.
  `create_chat_completion` calls OpenAI's chat completions endpoint directly when a
  BYOK key is resolved (stdlib `urllib`, same as the proxy call — no new dependency).
  Proxy path unchanged. `is_available()` returns True if any of: env key, saved key, or
  logged-in+configured.
- **`cli/cmd_key.py`** (new): `argus key set` (prompt hidden input or accept arg),
  `argus key show` (masked), `argus key clear`. Register in `cli/main.py`.
- **`embedding_store.py`**: resolve the OpenAI key via `user_config` (env → saved);
  skip embedding-based dedup gracefully if no key.

### 2. Secrets → env (`cloud.py`, `cmd_login.py`, `cmd_open_ui.py`, `cmd_doctor.py`)

- **`cloud.py`**: `SUPABASE_URL = os.environ.get("ARGUS_SUPABASE_URL")`,
  `SUPABASE_ANON_KEY = os.environ.get("ARGUS_SUPABASE_ANON_KEY")`, default `None`.
  When unset, all cloud functions return their existing no-op value
  (`is_logged_in()`→False, `push_run()`→False, `pull_shared_signatures()`→[]).
  No hardcoded Supabase URL remains in `src/argus/`.
- **`cmd_login.py`**: if `ARGUS_SUPABASE_URL` is unset, print a clear "cloud login is a
  hosted-only feature" message instead of attempting/crashing.
- **`cmd_open_ui.py`** (lines ~1127-1156): the report-upload path reads
  `SUPABASE_URL`/`SUPABASE_ANON_KEY` directly — guard it so local-only UI works when
  unset.
- **`cmd_doctor.py`**: report LLM mode (`BYOK (env)` / `BYOK (saved)` / `hosted` /
  `heuristic-only`) and storage mode (`local` / `cloud`).
- `feedback_store.py`, `storage.py`, `registry.py`, `candidate_store.py`: already lazy
  and no-op; verify no behavior change when unset (no edits expected beyond confirming).

### 3. `cloud/` folder + build exclusion (`cloud/config.py`, `pyproject.toml`)

- **`cloud/config.py`**: holds the real (public-safe) Supabase URL, anon key, and proxy
  URL, plus an `apply_env()` helper that sets the `ARGUS_SUPABASE_URL` /
  `ARGUS_SUPABASE_ANON_KEY` env vars if not already set. The hosted deployment imports
  and calls this at startup. OSS installs never ship `cloud/`, so it is never imported.
- `supabase/` stays where it is (already excluded from sdist; contains the proxy edge
  function and migrations).
- **`pyproject.toml`**: add `"cloud"` to the `[tool.hatch.build.targets.sdist]` exclude
  list. The wheel already only packages `src/argus`, so `cloud/` is already
  wheel-excluded — verify.

### 4. Dual licensing (`LICENSE`, `cloud/LICENSE`, `pyproject.toml`, `README`)

- **Root `LICENSE`** → Apache-2.0, with a short header note: "The `cloud/` directory is
  proprietary and licensed separately — see `cloud/LICENSE`."
- **`cloud/LICENSE`** → proprietary ("all rights reserved; commercial use requires a
  separate agreement").
- **`pyproject.toml`**: set `license = "Apache-2.0"` and the appropriate classifier.
- **`README`**: one-line licensing note.

## Verification (Phase 5 acceptance)

1. Fresh venv → `pip install -e .` → package installs without `cloud/`.
2. `argus key set` saves a key; `OPENAI_API_KEY=sk-... argus doctor` and the saved-key
   path both report BYOK working.
3. Run a LangGraph pipeline wrapped by ARGUS → detection pipeline works via BYOK.
4. `argus show <run-id>` and `argus ui` work from local storage only.
5. With no key and not logged in → heuristic-only detection, no crashes, no calls to
   ARGUS Supabase.
6. Grep: zero references to the ARGUS Supabase URL in `src/argus/`.
7. All 367 existing tests pass; new tests cover key resolution order, local key
   persistence, and cloud no-op when unconfigured.
8. Hosted deployment (full repo + `cloud/config.py apply_env()`) behaves exactly as
   today.

## Out of scope

- Anthropic BYOK.
- BYO-Supabase for OSS users.
- Plugin/interface abstraction layer (`cloud/auth.py`, `cloud/sync.py`).
- Any change to the detection pipeline, inspector, watcher, registry, or models.
