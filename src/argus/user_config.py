"""Local user config for BYOK (bring-your-own-key), multi-provider.

Stores per-provider API keys and the active provider at ~/.argus/config.json
(chmod 600) so they persist across sessions. Environment variables always win
over saved keys (12-factor override).

Config shape::

    {"provider": "anthropic",
     "keys": {"openai": "...", "anthropic": "...", "google": "..."},
     "models": {"cheap": "...", "capable": "..."}}

Legacy files with a top-level ``openai_api_key`` are read transparently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from argus.providers import PROVIDERS

_CONFIG_DIR = Path.home() / ".argus"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

# Env var names per provider (checked before the saved key).
_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _read() -> dict[str, Any]:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        return data
    except Exception:
        return {}


def _write(data: dict[str, Any]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _CONFIG_FILE.chmod(0o600)


def _saved_keys(data: dict[str, Any]) -> dict[str, str]:
    """Return saved keys, migrating a legacy top-level openai_api_key."""
    keys = dict(data.get("keys", {}))
    legacy = data.get("openai_api_key")
    if legacy and "openai" not in keys:
        keys["openai"] = legacy
    return keys


# ── Multi-provider API ──────────────────────────────────────────────────────


def get_saved_key(provider: str) -> str | None:
    """Return the saved key for a provider, or None."""
    return _saved_keys(_read()).get(provider) or None


def set_key(provider: str, key: str) -> None:
    """Persist a provider's key to ~/.argus/config.json (chmod 600)."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}. Choose from {', '.join(PROVIDERS)}.")
    data = _read()
    keys = _saved_keys(data)
    keys[provider] = key.strip()
    data["keys"] = keys
    data.pop("openai_api_key", None)  # collapse legacy field into keys
    _write(data)


def clear_key(provider: str | None = None) -> None:
    """Remove one provider's saved key, or all keys when provider is None."""
    data = _read()
    if provider is None:
        data.pop("keys", None)
        data.pop("openai_api_key", None)
    else:
        keys = _saved_keys(data)
        keys.pop(provider, None)
        data["keys"] = keys
        data.pop("openai_api_key", None)
    _write(data)


def resolve_key(provider: str) -> str | None:
    """Resolve a provider's key: env var(s) first, then saved key."""
    for env_name in _ENV_VARS.get(provider, ()):
        val = os.environ.get(env_name)
        if val:
            return val
    return get_saved_key(provider)


def set_provider(provider: str) -> None:
    """Set the active LLM provider."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}. Choose from {', '.join(PROVIDERS)}.")
    data = _read()
    data["provider"] = provider
    _write(data)


def get_provider() -> str:
    """Return the active provider.

    Explicit selection wins; otherwise infer from whichever provider has a
    resolvable key (OpenAI first). Defaults to 'openai'.
    """
    explicit = _read().get("provider")
    if isinstance(explicit, str) and explicit in PROVIDERS:
        return explicit
    for provider in PROVIDERS:
        if resolve_key(provider):
            return provider
    return "openai"


def get_model_overrides() -> dict[str, str]:
    """Return the user's per-tier model overrides ({} if none)."""
    models = _read().get("models", {})
    return {k: v for k, v in models.items() if isinstance(v, str) and v}


def configured_providers() -> list[str]:
    """Providers that currently have a resolvable key."""
    return [p for p in PROVIDERS if resolve_key(p)]


# ── Backward-compatible OpenAI-only shims ───────────────────────────────────


def get_saved_openai_key() -> str | None:
    return get_saved_key("openai")


def set_openai_key(key: str) -> None:
    set_key("openai", key)


def clear_openai_key() -> None:
    clear_key("openai")


def resolve_openai_key() -> str | None:
    return resolve_key("openai")
