"""Local user config for BYOK (bring-your-own-key).

Stores the user's OpenAI API key at ~/.argus/config.json (chmod 600) so it
persists across sessions. The environment variable OPENAI_API_KEY always wins
over the saved key (12-factor override).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path.home() / ".argus"
_CONFIG_FILE = _CONFIG_DIR / "config.json"


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


def get_saved_openai_key() -> str | None:
    """Return the OpenAI key saved on disk, or None."""
    key = _read().get("openai_api_key")
    return key or None


def set_openai_key(key: str) -> None:
    """Persist the OpenAI key to ~/.argus/config.json (chmod 600)."""
    data = _read()
    data["openai_api_key"] = key.strip()
    _write(data)


def clear_openai_key() -> None:
    """Remove the saved OpenAI key."""
    data = _read()
    data.pop("openai_api_key", None)
    _write(data)


def resolve_openai_key() -> str | None:
    """Resolve the OpenAI key: env OPENAI_API_KEY first, then saved key."""
    env = os.environ.get("OPENAI_API_KEY")
    if env:
        return env
    return get_saved_openai_key()
