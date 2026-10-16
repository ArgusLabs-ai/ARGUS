"""LLM calls with multi-provider BYOK-first resolution.

Priority:
  1. explicit api_key arg / active-provider key (env or saved) -> call that
     provider directly (BYOK: OpenAI, Anthropic, or Google Gemini).
  2. logged in + Supabase configured + ``ARGUS_HOSTED_LLM=1`` -> route through
     the ARGUS proxy (hosted/enterprise path; OpenAI-only). Opt-in because the
     prompts carry real ledger values, which then pass through ARGUS servers.
  3. none of the above -> {"error": ...}; callers fall back to
     heuristic-only detection.

Exposes create_chat_completion() mirroring the OpenAI chat completions shape.
Uses only stdlib urllib — no new dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from argus import providers, user_config
from argus.cloud import SUPABASE_URL, _get_valid_credentials, hosted_llm_enabled

_PROXY_URL = f"{SUPABASE_URL}/functions/v1/llm-proxy" if SUPABASE_URL else None


def _call_proxy(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    response_format: dict[str, str] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call the ARGUS LLM proxy edge function (hosted deployments only).

    ponytail: the proxy speaks plain chat completions, so ``tools``/
    ``tool_choice`` are dropped here rather than rejected — the judge prompts
    already carry the full JSON contract, and callers fall back to parsing
    ``content`` when the response carries no ``tool_calls``.
    """
    if not hosted_llm_enabled() or not SUPABASE_URL or _PROXY_URL is None:
        return {"error": "No LLM configured. Set OPENAI_API_KEY or run: argus key set"}

    creds = _get_valid_credentials()
    if creds is None:
        return {"error": "No LLM configured. Set OPENAI_API_KEY or run: argus key set"}

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    req = urllib.request.Request(
        _PROXY_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {creds.access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            result: dict[str, Any] = json.loads(resp.read())
            return result
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            return {"error": body.get("error", f"Proxy HTTP {exc.code}")}
        except Exception:
            return {"error": f"Proxy HTTP {exc.code}"}
    except Exception as exc:
        return {"error": f"Proxy error: {exc}"}


def _resolve_byok(api_key: str | None) -> tuple[str, str | None]:
    """Return (provider, key) for the direct BYOK path.

    An explicit api_key is treated as an OpenAI key (back-compat). Otherwise
    the active provider and its resolved key are used.
    """
    if api_key:
        return "openai", api_key
    provider = user_config.get_provider()
    return provider, user_config.resolve_key(provider)


def create_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    seed: int | None = None,
    response_format: dict[str, str] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Create a chat completion via BYOK direct call, else the hosted proxy.

    Returns a dict in the OpenAI response shape on success, or {"error": "..."}
    on failure. Callers should check for the "error" key. ``tools``/
    ``tool_choice`` follow the OpenAI chat-completions convention; adapters
    that cannot honor them (the hosted proxy) silently drop them so the caller
    can fall back to parsing message content. ``seed`` is the sampling seed: only the BYOK
    adapters get it, and only the reply they mark ``seed_honored`` was really seeded (the
    hosted proxy forwards no seed, so its replies never carry the mark).
    """
    provider, key = _resolve_byok(api_key)
    if key:
        adapter = providers.ADAPTERS[provider]
        actual_model = providers.resolve_model(
            provider, model, user_config.get_model_overrides()
        )
        # tools/tool_choice/seed are passed only when set so third-party adapter
        # stubs with the old signature keep working.
        extra: dict[str, Any] = {}
        if tools:
            extra["tools"] = tools
        if tool_choice:
            extra["tool_choice"] = tool_choice
        if seed is not None:
            extra["seed"] = seed
        return adapter(
            api_key=key,
            model=actual_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            timeout=timeout,
            **extra,
        )
    return _call_proxy(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        tools=tools,
        tool_choice=tool_choice,
        timeout=timeout,
    )


def is_available() -> bool:
    """True if any LLM path is usable: a BYOK key, or the logged-in proxy."""
    if user_config.configured_providers():
        return True
    if not hosted_llm_enabled() or not SUPABASE_URL:
        return False
    return _get_valid_credentials() is not None
