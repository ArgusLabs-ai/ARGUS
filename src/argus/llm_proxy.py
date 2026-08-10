"""LLM calls with BYOK-first resolution.

Priority:
  1. OPENAI_API_KEY env var         -> call OpenAI directly (BYOK)
  2. saved key (~/.argus/config)    -> call OpenAI directly (BYOK)
  3. logged in + Supabase configured -> route through the ARGUS proxy
  4. none of the above              -> {"error": ...}; callers fall back to
                                       heuristic-only detection.

Exposes create_chat_completion() mirroring the OpenAI chat completions shape.
Uses only stdlib urllib — no new dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from argus.cloud import SUPABASE_URL, _get_valid_credentials
from argus.user_config import resolve_openai_key

_PROXY_URL = f"{SUPABASE_URL}/functions/v1/llm-proxy" if SUPABASE_URL else None
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _call_openai_direct(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    response_format: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call OpenAI's chat completions endpoint directly with the user's key."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    req = urllib.request.Request(
        _OPENAI_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result: dict[str, Any] = json.loads(resp.read())
            return result
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            msg = body.get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
            return {"error": msg or f"OpenAI HTTP {exc.code}"}
        except Exception:
            return {"error": f"OpenAI HTTP {exc.code}"}
    except Exception as exc:
        return {"error": f"OpenAI error: {exc}"}


def _call_proxy(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    response_format: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call the ARGUS LLM proxy edge function (hosted deployments only)."""
    if not SUPABASE_URL or _PROXY_URL is None:
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def create_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    response_format: dict[str, str] | None = None,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Create a chat completion via BYOK direct call, else the hosted proxy.

    Returns the raw OpenAI response dict on success, or {"error": "..."} on
    failure. Callers should check for the "error" key.
    """
    key = api_key or resolve_openai_key()
    if key:
        return _call_openai_direct(
            api_key=key,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            timeout=timeout,
        )
    return _call_proxy(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        timeout=timeout,
    )


def is_available() -> bool:
    """True if any LLM path is usable: BYOK key, or logged-in hosted proxy."""
    if resolve_openai_key():
        return True
    if not SUPABASE_URL:
        return False
    return _get_valid_credentials() is not None
