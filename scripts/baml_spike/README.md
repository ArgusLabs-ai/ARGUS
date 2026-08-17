# BAML transport-fit spike (B1)

Throwaway harness that answers one question from the BAML adoption tracker:
**can a BAML-generated client talk to ARGUS's LLM transport?**

Read [FINDINGS.md](FINDINGS.md) for the answer. This file is just how to run it.

## Run

```bash
pip install baml-py==0.225.0
python scripts/baml_spike/run_spike.py --verbose
```

No API keys, no Supabase project, no network. The probes run against a local
replica of the transport contract.

## What's here

| File | |
|---|---|
| `run_spike.py` | The harness. Nine probes, prints a verdict table. |
| `fake_proxy.py` | Local replica of `supabase/functions/llm-proxy/index.ts` and of an OpenAI-compatible vendor API. Records what BAML put on the wire. |
| `baml_src/` | BAML client + function definitions under test. Mirrors the schema `semantic_checker.py` already asks for. |
| `FINDINGS.md` | The writeup. |
| `baml_client/` | Generated, gitignored. `run_spike.py` regenerates it (~0.1s). |

## Why a replica instead of live Supabase

B1 is a question about wire contracts — URL shape, auth header, error
envelope, which request fields survive. A replica pins all of those exactly
and makes the spike reproducible by anyone, on any branch, without a login or
a quota. `fake_proxy.py` reproduces `index.ts` path by path, including the
parts that differ from OpenAI: the bare `{"error": "..."}` failure envelope,
the 200-calls/day 429, and the fact that the function ignores the request path.

Two things a replica can't tell you, both out of scope for B1 and neither
blocking: whether real Supabase edge routing rejects the `/chat/completions`
subpath before the function runs (it routes on function name, so it shouldn't),
and how real models behave under BAML's prompt-embedded schema versus OpenAI
JSON mode. The second is a prompt-quality question for the pilot, not a
transport question.

## Status

Spike only. Adds no dependency to `pyproject.toml`, imports nothing from
`argus`, is imported by nothing in `argus`, and ships in neither the sdist nor
the wheel. Delete the directory when the tracker's §7 questions close.
