# ARGUS — `cloud/` (Hosted / Enterprise Layer)

> **Proprietary. Not open source.** This directory is **not** covered by the
> Apache-2.0 license that applies to `src/argus/`. See [`LICENSE`](./LICENSE) —
> all rights reserved; commercial use requires a separate agreement.
> It is **excluded from the PyPI package** (`argus-agents`) via `pyproject.toml`,
> so open-source `pip install` users never receive it.

This is the hosted/enterprise half of ARGUS's open-core split. The open-source
core in `src/argus/` is fully functional on its own (local storage, BYOK LLM,
heuristic + semantic detection). This layer adds the **managed cloud tier** on
top of that same codebase — no fork, no separate branch, just an import boundary.

## What this layer enables

Everything here is gated behind configuration; when it is absent (open-source
install), the core silently runs local-only.

| Capability | Open-source core | With `cloud/` configured |
|------------|------------------|--------------------------|
| Run storage | local `.argus/runs/*.json` | + sync to hosted Supabase |
| LLM calls | BYOK (user's own key) | hosted proxy (managed key, no BYOK needed) |
| Learned trends | local `custom_signatures.json` only | + shared community/team registry |
| Auth (`argus login`) | disabled (hosted-only feature) | Google OAuth via hosted Supabase |
| Dashboard | local, no account | account-backed, cloud runs |

## Contents

| File | Role |
|------|------|
| `config.py` | Hosted Supabase URL + anon key (RLS-protected, public-safe) and `PROXY_URL`; `apply_env()` exports them into the environment. |
| `LICENSE` | Proprietary license for this directory. |

The actual Supabase migrations, RLS policies, and the LLM-proxy edge function
live in the repo's top-level `supabase/` directory (also excluded from PyPI).
The real secret — the OpenAI key used by the proxy — lives **only** in Supabase
Edge Function secrets, never in this repo.

## How activation works

The core (`src/argus/cloud.py`) reads its Supabase config from two environment
variables and no-ops when they are unset:

- `ARGUS_SUPABASE_URL`
- `ARGUS_SUPABASE_ANON_KEY`

There are two ways they get set in a hosted deployment:

1. **Automatic (full-repo deployment).** `src/argus/__init__.py` attempts
   `from cloud.config import apply_env` on import and calls it. When the repo
   root is on `PYTHONPATH` (i.e. `cloud/` is importable), the hosted config is
   wired in automatically. In the open-source pip package `cloud/` is absent, so
   this import fails and is silently skipped.

2. **Explicit / container env.** Set `ARGUS_SUPABASE_URL` and
   `ARGUS_SUPABASE_ANON_KEY` directly in the deployment environment
   (Docker/systemd/Vercel/etc.). These take precedence and are the recommended
   approach for production, since they are read before any Python import.

```python
# Hosted entrypoint (full repo on path)
from cloud.config import apply_env
apply_env()          # ARGUS_SUPABASE_URL / _ANON_KEY now set if not already
import argus         # core picks up the hosted config
```

## Do not

- Do **not** move code here that the open-source core needs to function — the
  core must stay fully usable without `cloud/`.
- Do **not** hardcode real secrets (service-role keys, OpenAI keys) here. Only
  the public, RLS-protected anon key belongs in `config.py`.
- Do **not** relicense this directory under Apache-2.0.

See the repository [`README.md`](../README.md) for the open-source product and
BYOK usage.
