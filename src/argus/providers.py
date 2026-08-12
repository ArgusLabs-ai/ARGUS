"""Multi-provider LLM adapters (BYOK).

Each adapter takes OpenAI-shaped arguments and returns a response normalized
to the OpenAI chat-completions shape:

    {"choices": [{"message": {"content": "..."}}],
     "usage": {"prompt_tokens": int, "completion_tokens": int}}

or {"error": "..."} on failure. This lets every existing caller keep reading
``result["choices"][0]["message"]["content"]`` and ``result["usage"]``
regardless of which provider actually served the request.

Uses only stdlib urllib — no new dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

PROVIDERS = ("openai", "anthropic", "google")

# Per-provider model tiers. "cheap" = per-node/high-frequency checks,
# "capable" = correlation / final judge. Overridable via user config.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "openai": {"cheap": "gpt-4o-mini", "capable": "gpt-4o"},
    "anthropic": {"cheap": "claude-3-5-haiku-latest", "capable": "claude-sonnet-4-5"},
    "google": {"cheap": "gemini-2.5-flash", "capable": "gemini-2.5-pro"},
}

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_CHEAP_MARKERS = ("mini", "flash", "haiku")


def provider_for_model(model: str) -> str:
    """Infer the provider that owns a model id from its name."""
    m = model.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini"):
        return "google"
    return "openai"  # gpt-*, o1/o3/o4-*, and unknown default to OpenAI


def tier_for_model(model: str) -> str:
    """Classify an OpenAI model name as the 'cheap' or 'capable' tier."""
    m = model.lower()
    return "cheap" if any(marker in m for marker in _CHEAP_MARKERS) else "capable"


def resolve_model(
    provider: str, requested_model: str, overrides: dict[str, str] | None = None
) -> str:
    """Map a caller's (OpenAI) model name to the active provider's model.

    ponytail: tier is inferred from the OpenAI model name (mini/flash/haiku =
    cheap, else capable) because callers pass hardcoded OpenAI names as tier
    hints. Upgrade path: thread an explicit ``tier`` argument through
    create_chat_completion instead of guessing from the name.
    """
    if provider == "openai":
        return requested_model  # pass through — zero regression for OpenAI users
    tier = tier_for_model(requested_model)
    overrides = overrides or {}
    return overrides.get(tier) or DEFAULT_MODELS[provider][tier]


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    error_label: str,
) -> dict[str, Any]:
    """POST JSON and return the parsed body, or {"error": ...}."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body: dict[str, Any] = json.loads(resp.read())
            return body
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
            msg = err.get("error", {})
            if isinstance(msg, dict):
                msg = msg.get("message")
            return {"error": msg or f"{error_label} HTTP {exc.code}"}
        except Exception:
            return {"error": f"{error_label} HTTP {exc.code}"}
    except Exception as exc:
        return {"error": f"{error_label} error: {exc}"}


def _split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Separate system messages (concatenated) from the conversation."""
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    convo = [m for m in messages if m.get("role") != "system"]
    return "\n\n".join(system_parts), convo


def _envelope(content: str, prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    """Wrap provider output in the OpenAI response shape callers expect."""
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def call_openai(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    response_format: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call OpenAI chat completions. Response already matches the target shape."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format
    return _post_json(
        _OPENAI_URL,
        {"Authorization": f"Bearer {api_key}"},
        payload,
        timeout,
        "OpenAI",
    )


def call_anthropic(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    response_format: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call Anthropic Messages API and normalize to the OpenAI shape.

    ponytail: Anthropic has no json_object response_format; the callers'
    system prompts already instruct JSON output, so response_format is ignored.
    """
    system, convo = _split_system(messages)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": m["role"], "content": m["content"]} for m in convo],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system

    body = _post_json(
        _ANTHROPIC_URL,
        {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
        payload,
        timeout,
        "Anthropic",
    )
    if "error" in body:
        return body

    blocks = body.get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    usage = body.get("usage", {})
    return _envelope(
        text,
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
    )


def call_google(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2000,
    temperature: float = 0.3,
    response_format: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call Google Gemini generateContent and normalize to the OpenAI shape."""
    system, convo = _split_system(messages)
    contents = [
        {
            "role": "model" if m.get("role") == "assistant" else "user",
            "parts": [{"text": m["content"]}],
        }
        for m in convo
    ]
    generation_config: dict[str, Any] = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
    }
    if response_format and response_format.get("type") == "json_object":
        generation_config["responseMimeType"] = "application/json"

    payload: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    url = _GOOGLE_URL.format(model=model)
    body = _post_json(
        url,
        {"x-goog-api-key": api_key},
        payload,
        timeout,
        "Gemini",
    )
    if "error" in body:
        return body

    candidates = body.get("candidates", [])
    text = ""
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
    usage = body.get("usageMetadata", {})
    return _envelope(
        text,
        usage.get("promptTokenCount", 0),
        usage.get("candidatesTokenCount", 0),
    )


ADAPTERS = {
    "openai": call_openai,
    "anthropic": call_anthropic,
    "google": call_google,
}
