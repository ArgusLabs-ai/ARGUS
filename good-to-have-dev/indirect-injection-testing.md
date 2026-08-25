# Indirect prompt injection & tool hijack testing

**Status:** not built · **Verdict:** strongest differentiator · **Size:** medium

## The idea

Most LLM security testing treats the agent as a chat box: inject "ignore previous instructions"
into the user prompt and see what happens. But an agent is a network of tools, and the dangerous
payload usually is not typed by the user — it is *fetched*.

The scenario: the agent uses a tool to read a webpage. The page contains hidden text saying
"delete the user account." Does the agent execute an instruction it found in tool output?

Simulating malicious *tool returns* — data-plane injection, not prompt-plane — is something no
observability tool on the market does.

## What already exists

The injection primitive is already built, for a completely different reason.

- **`session.frozen_outputs`** (`session.py:260`, popped in `_pop_frozen_output`) substitutes a
  recorded output for a node instead of calling it. It exists to freeze upstream nodes during
  replay — but "return this payload instead of calling the real function" is exactly a mock tool
  response. Feeding it an adversarial payload rather than a recorded one is a change of intent,
  not of mechanism.
- **`http_recorder.py`** intercepts outbound HTTP at the `urllib3` layer, underneath `requests`
  and `httpx`. `playback_http()` already serves canned responses. Injecting a malicious HTTP
  *response body* needs no new interception machinery.
- **State patching** (`state_patch.py`) can now place an arbitrary payload into any field of a
  recorded state before resuming — a third injection surface, already shipped.
- **Detection partially exists.** `heuristic_engine.py` and the 150+ signature registry
  (`registry.py`) already scan outputs for degradation patterns; `ToolFailure` models tool-level
  problems.

## What's genuinely missing

1. **A payload corpus** — known indirect-injection patterns, versioned and updatable.
2. **A harness** that runs a pipeline once per payload and records what the agent did. This is the
   dependency on [smoke-test-runner.md](smoke-test-runner.md).
3. **A judgement layer** — the hard part. Detecting that the agent *complied* with an injected
   instruction is not pattern matching on the output; it requires knowing which tool calls were
   attempted and whether any was attributable to injected text rather than user intent.
   `llm_tracker.py` records LLM calls, but there is no tool-call intent record today.
4. **A report** that distinguishes "agent ignored it" / "agent repeated it" / "agent acted on it".
   Only the third is a real vulnerability, and conflating them produces noise.

## Why it's parked

It depends on the smoke runner, and (3) is genuinely unsolved rather than merely unbuilt.

It is worth flagging as the highest-*value* item here even though it is not the highest-*priority*.
"The only observability tool that tests for data-plane prompt injection" is a stronger market
claim than anything else in this folder, and security review is a hard gate on agents touching
production systems.

One caution: this feature involves generating and storing adversarial payloads. Keep the corpus
clearly scoped to defensive testing of the user's own pipelines, and keep payloads inert as data
(never executed by ARGUS itself).
