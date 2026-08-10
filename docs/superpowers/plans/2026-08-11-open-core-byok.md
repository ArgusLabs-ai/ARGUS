# Open-Core Separation + BYOK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pip install argus-agents` a fully working BYOK product with no ARGUS infra dependency, while keeping the hosted/enterprise deployment working via env vars, all in one public repo.

**Architecture:** The open-source core (`src/argus/`) resolves an OpenAI key from env or a locally-saved config file and calls OpenAI directly; if neither exists it falls back to the hosted proxy (only when Supabase env vars are configured), else to heuristic-only detection. Supabase URL/key move out of the source into env vars (real values live in the git-tracked-but-PyPI-excluded `cloud/` folder). The repo is dual-licensed: Apache-2.0 for `src/argus/`, proprietary for `cloud/`.

**Tech Stack:** Python 3.9+, stdlib `urllib` (no new deps), Typer CLI, pytest, hatchling build.

**Spec:** `docs/superpowers/specs/2026-08-11-open-core-byok-design.md`

---

## File map

| File | Responsibility | Action |
|------|----------------|--------|
| `src/argus/user_config.py` | Load/save local BYOK OpenAI key; resolve env-or-saved key | Create |
| `src/argus/llm_proxy.py` | BYOK-direct-or-proxy LLM calls | Modify |
| `src/argus/embedding_store.py` | Use resolved key for OpenAI client | Modify |
| `src/argus/cloud.py` | Supabase URL/key from env, no-op when unset | Modify |
| `src/argus/cli/cmd_key.py` | `argus key set/show/clear` | Create |
| `src/argus/cli/main.py` | Register `key` command | Modify |
| `src/argus/cli/cmd_login.py` | Hosted-only guard when Supabase unset | Modify |
| `src/argus/cli/cmd_open_ui.py` | Guard report upload when Supabase unset | Modify |
| `src/argus/cli/cmd_doctor.py` | Report LLM/storage mode | Modify |
| `cloud/config.py` | Real Supabase URL/anon key/proxy + `apply_env()` | Create |
| `cloud/LICENSE` | Proprietary license for `cloud/` | Create |
| `LICENSE` | Apache-2.0 + pointer to `cloud/LICENSE` | Replace |
| `pyproject.toml` | Exclude `cloud/`, set Apache-2.0 license | Modify |
| `tests/test_smoke.py` | New tests for all above | Modify |

---

## Task 1: Local BYOK key store (`user_config.py`)

**Files:**
- Create: `src/argus/user_config.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
def test_user_config_save_load_clear(tmp_path, monkeypatch):
    import argus.user_config as uc

    monkeypatch.setattr(uc, "_CONFIG_DIR", tmp_path / ".argus")
    monkeypatch.setattr(uc, "_CONFIG_FILE", tmp_path / ".argus" / "config.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert uc.get_saved_openai_key() is None
    uc.set_openai_key("sk-test-123")
    assert uc.get_saved_openai_key() == "sk-test-123"
    # file is chmod 600
    import stat
    mode = stat.S_IMODE((tmp_path / ".argus" / "config.json").stat().st_mode)
    assert mode == 0o600
    uc.clear_openai_key()
    assert uc.get_saved_openai_key() is None


@pytest.mark.unit
def test_resolve_openai_key_prefers_env(tmp_path, monkeypatch):
    import argus.user_config as uc

    monkeypatch.setattr(uc, "_CONFIG_DIR", tmp_path / ".argus")
    monkeypatch.setattr(uc, "_CONFIG_FILE", tmp_path / ".argus" / "config.json")
    uc.set_openai_key("sk-saved")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert uc.resolve_openai_key() == "sk-env"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert uc.resolve_openai_key() == "sk-saved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smoke.py::test_user_config_save_load_clear -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'argus.user_config'`

- [ ] **Step 3: Write minimal implementation**

Create `src/argus/user_config.py`:

```python
"""Local user config for BYOK (bring-your-own-key).

Stores the user's OpenAI API key at ~/.argus/config.json (chmod 600) so it
persists across sessions. The environment variable OPENAI_API_KEY always wins
over the saved key (12-factor override).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_CONFIG_DIR = Path.home() / ".argus"
_CONFIG_FILE = _CONFIG_DIR / "config.json"


def _read() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data: dict) -> None:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_smoke.py::test_user_config_save_load_clear tests/test_smoke.py::test_resolve_openai_key_prefers_env -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/argus/user_config.py tests/test_smoke.py
git commit -m "feat: add local BYOK openai key store (user_config)"
```

---

## Task 2: BYOK path in `llm_proxy.py`

**Files:**
- Modify: `src/argus/llm_proxy.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
def test_llm_proxy_uses_byok_when_key_present(monkeypatch):
    import argus.llm_proxy as lp

    captured = {}

    def fake_direct(*, api_key, model, messages, max_tokens, temperature,
                    response_format, timeout):
        captured["api_key"] = api_key
        captured["model"] = model
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(lp, "_call_openai_direct", fake_direct)
    monkeypatch.setattr(lp, "resolve_openai_key", lambda: "sk-byok")

    out = lp.create_chat_completion(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert "error" not in out
    assert captured["api_key"] == "sk-byok"
    assert captured["model"] == "gpt-4o-mini"


@pytest.mark.unit
def test_llm_proxy_errors_when_no_key_and_no_proxy(monkeypatch):
    import argus.llm_proxy as lp

    monkeypatch.setattr(lp, "resolve_openai_key", lambda: None)
    monkeypatch.setattr(lp, "SUPABASE_URL", None)

    out = lp.create_chat_completion(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert "error" in out


@pytest.mark.unit
def test_llm_proxy_is_available_true_with_byok(monkeypatch):
    import argus.llm_proxy as lp

    monkeypatch.setattr(lp, "resolve_openai_key", lambda: "sk-byok")
    assert lp.is_available() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smoke.py::test_llm_proxy_uses_byok_when_key_present -v`
Expected: FAIL with `AttributeError: ... has no attribute '_call_openai_direct'`

- [ ] **Step 3: Write implementation — replace `src/argus/llm_proxy.py` entirely**

```python
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
            return json.loads(resp.read())
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
            return json.loads(resp.read())
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_smoke.py -k "llm_proxy" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/argus/llm_proxy.py tests/test_smoke.py
git commit -m "feat: BYOK-first LLM resolution in llm_proxy"
```

---

## Task 3: Supabase config from env in `cloud.py`

**Files:**
- Modify: `src/argus/cloud.py:17-24` (constants), `is_logged_in`, `_get_valid_credentials`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
def test_cloud_no_hardcoded_supabase_url():
    import inspect
    import argus.cloud as cloud
    src = inspect.getsource(cloud)
    assert "isnphpbckxfjsxllryrg" not in src  # real project ref must be gone


@pytest.mark.unit
def test_cloud_noops_when_unconfigured(monkeypatch):
    import argus.cloud as cloud
    monkeypatch.setattr(cloud, "SUPABASE_URL", None)
    monkeypatch.setattr(cloud, "SUPABASE_ANON_KEY", None)
    assert cloud.is_logged_in() is False
    assert cloud.push_run({"run_id": "x"}) is False
    assert cloud.pull_shared_signatures() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smoke.py::test_cloud_no_hardcoded_supabase_url -v`
Expected: FAIL (URL still hardcoded)

- [ ] **Step 3: Edit `src/argus/cloud.py`**

Replace lines 17-24 (the hardcoded constants block):

```python
# ── Supabase config (hosted deployments set these via cloud/config.py) ────
import os  # noqa: E402

SUPABASE_URL = os.environ.get("ARGUS_SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("ARGUS_SUPABASE_ANON_KEY")
```

Replace `is_logged_in` (currently lines 75-77):

```python
def is_logged_in() -> bool:
    if SUPABASE_URL is None:
        return False
    creds = load_credentials()
    return creds is not None
```

Add a guard at the top of `_get_valid_credentials` (currently lines 115-119):

```python
def _get_valid_credentials() -> Credentials | None:
    if SUPABASE_URL is None:
        return None
    creds = load_credentials()
    if creds is None:
        return None
    return _refresh_if_needed(creds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_smoke.py -k "cloud" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/argus/cloud.py tests/test_smoke.py
git commit -m "refactor: read Supabase config from env, no-op when unset"
```

---

## Task 4: Guard hosted-only CLI paths (`cmd_login.py`, `cmd_open_ui.py`)

**Files:**
- Modify: `src/argus/cli/cmd_login.py` (`login` function, ~line 80)
- Modify: `src/argus/cli/cmd_open_ui.py:1127-1156` (report upload block)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
def test_login_reports_hosted_only_when_unconfigured(monkeypatch, capsys):
    import argus.cli.cmd_login as cl
    monkeypatch.setattr(cl, "SUPABASE_URL", None)
    cl.login()
    out = capsys.readouterr().out.lower()
    assert "hosted" in out or "not available" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smoke.py::test_login_reports_hosted_only_when_unconfigured -v`
Expected: FAIL (login tries to open browser / build auth URL against None)

- [ ] **Step 3: Edit `login()` in `src/argus/cli/cmd_login.py`**

Add at the very top of the `login()` function body (before the existing `if is_logged_in():` check around line 82):

```python
    if SUPABASE_URL is None:
        _console.print(
            "  [yellow]Cloud login is a hosted-only feature.[/yellow]\n"
            "  ARGUS runs fully local with your own key — set one with: "
            "[bold]argus key set[/bold]\n"
            "  or export OPENAI_API_KEY."
        )
        return
```

- [ ] **Step 4: Edit `cmd_open_ui.py` report-upload block (lines ~1127-1156)**

Wrap the block that imports and uses `SUPABASE_URL`/`SUPABASE_ANON_KEY` so it is skipped when unset. Immediately before the existing `from argus.cloud import (SUPABASE_ANON_KEY, SUPABASE_URL)` import at line ~1127, add:

```python
                from argus.cloud import SUPABASE_URL as _sb_url

                if _sb_url is None:
                    return  # local-only mode: report upload is hosted-only
```

(Keep the existing block below it unchanged; it only runs when a URL is configured.)

- [ ] **Step 5: Run tests + commit**

Run: `pytest tests/test_smoke.py -k "login" -v`
Expected: PASS

```bash
git add src/argus/cli/cmd_login.py src/argus/cli/cmd_open_ui.py tests/test_smoke.py
git commit -m "fix: guard hosted-only CLI paths when Supabase unconfigured"
```

---

## Task 5: `argus key` command (`cmd_key.py` + register)

**Files:**
- Create: `src/argus/cli/cmd_key.py`
- Modify: `src/argus/cli/main.py` (import + command registration)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
def test_cmd_key_set_show_clear(tmp_path, monkeypatch, capsys):
    import argus.user_config as uc
    import argus.cli.cmd_key as ck

    monkeypatch.setattr(uc, "_CONFIG_DIR", tmp_path / ".argus")
    monkeypatch.setattr(uc, "_CONFIG_FILE", tmp_path / ".argus" / "config.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    ck.key_set("sk-abcdef123456")
    assert uc.get_saved_openai_key() == "sk-abcdef123456"

    ck.key_show()
    out = capsys.readouterr().out
    assert "sk-abcdef123456" not in out  # masked
    assert "3456" in out  # last 4 shown

    ck.key_clear()
    assert uc.get_saved_openai_key() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smoke.py::test_cmd_key_set_show_clear -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'argus.cli.cmd_key'`

- [ ] **Step 3: Create `src/argus/cli/cmd_key.py`**

```python
"""argus key — manage the local BYOK OpenAI API key."""

from __future__ import annotations

from rich.console import Console

from argus import user_config

_console = Console()


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}…{key[-4:]}"


def key_set(value: str | None = None) -> None:
    """Save an OpenAI key locally (prompts if not passed)."""
    if not value:
        import getpass

        value = getpass.getpass("OpenAI API key (input hidden): ").strip()
    if not value:
        _console.print("  [red]No key entered.[/red]")
        return
    user_config.set_openai_key(value)
    _console.print(f"  [green]Saved[/green] key {_mask(value)} to ~/.argus/config.json")


def key_show() -> None:
    """Show the currently resolved key (masked) and its source."""
    import os

    env = os.environ.get("OPENAI_API_KEY")
    saved = user_config.get_saved_openai_key()
    if env:
        _console.print(f"  key: {_mask(env)}  [dim](source: OPENAI_API_KEY env)[/dim]")
    elif saved:
        _console.print(f"  key: {_mask(saved)}  [dim](source: ~/.argus/config.json)[/dim]")
    else:
        _console.print(
            "  [yellow]No OpenAI key set.[/yellow] Run [bold]argus key set[/bold] "
            "or export OPENAI_API_KEY."
        )


def key_clear() -> None:
    """Remove the saved local key."""
    user_config.clear_openai_key()
    _console.print("  [green]Cleared[/green] saved key from ~/.argus/config.json")
```

- [ ] **Step 4: Register in `src/argus/cli/main.py`**

Add import after line 21 (`from argus.cli.cmd_login import ...`):

```python
from argus.cli.cmd_key import key_clear, key_set, key_show
```

Add a Typer sub-app registration after line 37 (`app.add_typer(open_app, name="open")`):

```python
key_app = typer.Typer(help="Manage your BYOK OpenAI API key.", no_args_is_help=True)
app.add_typer(key_app, name="key")


@key_app.command("set")
def cmd_key_set(
    value: Annotated[
        Optional[str],
        typer.Argument(help="OpenAI API key. Omit to be prompted with hidden input."),
    ] = None,
) -> None:
    """Save your OpenAI API key locally (~/.argus/config.json)."""
    key_set(value)


@key_app.command("show")
def cmd_key_show() -> None:
    """Show the currently resolved key (masked) and its source."""
    key_show()


@key_app.command("clear")
def cmd_key_clear() -> None:
    """Remove the saved local key."""
    key_clear()
```

Also add to the `_COMMANDS` help list (after the `"whoami"` entry, line ~70):

```python
    ("key set", "save your OpenAI API key locally for BYOK mode"),
```

- [ ] **Step 5: Run tests + smoke-check CLI wiring**

Run: `pytest tests/test_smoke.py::test_cmd_key_set_show_clear -v`
Expected: PASS

Run: `argus key --help`
Expected: shows `set`, `show`, `clear` subcommands

- [ ] **Step 6: Commit**

```bash
git add src/argus/cli/cmd_key.py src/argus/cli/main.py tests/test_smoke.py
git commit -m "feat: add 'argus key' command for local BYOK key management"
```

---

## Task 6: `embedding_store` uses resolved key

**Files:**
- Modify: `src/argus/embedding_store.py:30-47` (`_get_client`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
def test_embedding_client_uses_resolved_key(monkeypatch):
    import argus.embedding_store as es

    captured = {}

    class FakeOpenAI:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

    monkeypatch.setattr(es, "_client", None)
    monkeypatch.setattr("argus.user_config.resolve_openai_key", lambda: "sk-embed")
    import sys, types
    fake_mod = types.ModuleType("openai")
    fake_mod.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_mod)

    es._get_client()
    assert captured["api_key"] == "sk-embed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smoke.py::test_embedding_client_uses_resolved_key -v`
Expected: FAIL (client built with `OpenAI()` — no api_key passed)

- [ ] **Step 3: Edit `_get_client` in `src/argus/embedding_store.py`**

Replace the body after the dotenv block (the `from openai import OpenAI` / `_client = OpenAI()` lines, ~44-47) with:

```python
        from openai import OpenAI  # noqa: PLC0415

        from argus.user_config import resolve_openai_key  # noqa: PLC0415

        key = resolve_openai_key()
        _client = OpenAI(api_key=key) if key else OpenAI()
        return _client
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_smoke.py::test_embedding_client_uses_resolved_key -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/argus/embedding_store.py tests/test_smoke.py
git commit -m "feat: embedding_store uses BYOK-resolved OpenAI key"
```

---

## Task 7: `doctor` reports LLM + storage mode

**Files:**
- Modify: `src/argus/cli/cmd_doctor.py` (add `_check_llm_mode`, register in `checks`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
def test_doctor_llm_mode_byok(monkeypatch):
    import argus.cli.cmd_doctor as d
    monkeypatch.setattr("argus.user_config.resolve_openai_key", lambda: "sk-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    ok, msg = d._check_llm_mode()
    assert ok is True
    assert "BYOK" in msg


@pytest.mark.unit
def test_doctor_llm_mode_heuristic(monkeypatch):
    import argus.cli.cmd_doctor as d
    monkeypatch.setattr("argus.user_config.resolve_openai_key", lambda: None)
    monkeypatch.setattr("argus.cloud.SUPABASE_URL", None)
    ok, msg = d._check_llm_mode()
    assert "heuristic" in msg.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smoke.py::test_doctor_llm_mode_byok -v`
Expected: FAIL with `AttributeError: ... no attribute '_check_llm_mode'`

- [ ] **Step 3: Add `_check_llm_mode` to `src/argus/cli/cmd_doctor.py`**

Insert before `def doctor()` (line ~183):

```python
def _check_llm_mode() -> tuple[bool, str]:
    """Report which LLM path is active: BYOK / hosted / heuristic-only."""
    import os

    from argus.cloud import SUPABASE_URL
    from argus.user_config import resolve_openai_key

    if resolve_openai_key():
        source = "env" if os.environ.get("OPENAI_API_KEY") else "saved"
        return True, f"BYOK ({source}) — calling OpenAI directly"
    if SUPABASE_URL is not None:
        from argus.cloud import is_logged_in

        if is_logged_in():
            return True, "hosted proxy (logged in)"
        return True, "hosted available — run: argus login (or set OPENAI_API_KEY)"
    return True, "heuristic-only — no key set (argus key set to enable LLM checks)"
```

Register it in the `checks` list inside `doctor()` (after the `("storage", _check_storage)` entry):

```python
        ("llm", _check_llm_mode),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_smoke.py -k "doctor_llm_mode" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/argus/cli/cmd_doctor.py tests/test_smoke.py
git commit -m "feat: argus doctor reports BYOK/hosted/heuristic LLM mode"
```

---

## Task 8: Create `cloud/` folder (config + license)

**Files:**
- Create: `cloud/config.py`
- Create: `cloud/LICENSE`
- Create: `cloud/__init__.py` (empty)

- [ ] **Step 1: Create `cloud/config.py`**

```python
"""Hosted/enterprise Supabase configuration.

NOT shipped in the PyPI package (excluded via pyproject.toml). The hosted
deployment imports this and calls apply_env() at startup so that
src/argus/cloud.py picks up the Supabase config from the environment.

The anon key is RLS-protected and safe to embed; the real secret (the OpenAI
key used by the proxy) lives only in Supabase Edge Function secrets.
"""

from __future__ import annotations

import os

SUPABASE_URL = "https://isnphpbckxfjsxllryrg.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlzbnBocGJja3hmanN4bGxyeXJnIiwi"
    "cm9sZSI6ImFub24iLCJpYXQiOjE3Nzc0MDcxNjQsImV4cCI6MjA5Mjk4MzE2NH0."
    "nfaMxbDL9E8gyvV7k2r6F8PQhAcxdZ4QVlkVWloJ88Q"
)
PROXY_URL = f"{SUPABASE_URL}/functions/v1/llm-proxy"


def apply_env() -> None:
    """Export hosted Supabase config into the environment if not already set."""
    os.environ.setdefault("ARGUS_SUPABASE_URL", SUPABASE_URL)
    os.environ.setdefault("ARGUS_SUPABASE_ANON_KEY", SUPABASE_ANON_KEY)
```

- [ ] **Step 2: Create `cloud/__init__.py`** (empty file)

```bash
touch cloud/__init__.py
```

- [ ] **Step 3: Create `cloud/LICENSE`** — copy the current proprietary LICENSE text

```bash
cp LICENSE cloud/LICENSE
```

(The root `LICENSE` becomes Apache-2.0 in Task 9; `cloud/LICENSE` retains the proprietary terms.)

- [ ] **Step 4: Commit**

```bash
git add cloud/config.py cloud/__init__.py cloud/LICENSE
git commit -m "feat: add cloud/ hosted config + proprietary license"
```

---

## Task 9: Dual-license + build exclusion (`LICENSE`, `pyproject.toml`)

**Files:**
- Replace: `LICENSE` (Apache-2.0 + note)
- Modify: `pyproject.toml` (license field, classifier, sdist exclude)

- [ ] **Step 1: Replace root `LICENSE` with Apache-2.0**

Write the full Apache License 2.0 text (from https://www.apache.org/licenses/LICENSE-2.0.txt) into `LICENSE`, prefixed with this note as the first lines:

```
ARGUS is open-core. The open-source core in src/argus/ is licensed under the
Apache License, Version 2.0 (below). The cloud/ directory (hosted/enterprise
components) is proprietary and licensed separately — see cloud/LICENSE.

Copyright (c) 2026 Varad Durge

--------------------------------------------------------------------------------

                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/
... (full Apache 2.0 body) ...
```

- [ ] **Step 2: Edit `pyproject.toml`**

Change the license line (line 6):

```toml
license = "Apache-2.0"
```

Change the license classifier (in `classifiers`, replace `"License :: Other/Proprietary License",`):

```toml
    "License :: OSI Approved :: Apache Software License",
```

Add `"cloud"` to the sdist exclude list (line ~65):

```toml
exclude = ["website", "assets", "fixtures", "supabase", "cloud", "scripts", ".github"]
```

- [ ] **Step 3: Verify the build excludes `cloud/`**

Run:
```bash
python -m build --sdist 2>/dev/null || pip install build && python -m build --sdist
tar -tzf dist/argus_agents-*.tar.gz | grep -c "cloud/" || echo "cloud/ excluded: OK"
```
Expected: `cloud/ excluded: OK` (grep finds 0 → prints the OK message)

Also confirm the wheel excludes it:
```bash
python -m build --wheel
unzip -l dist/argus_agents-*.whl | grep -c "cloud/" || echo "wheel cloud/ excluded: OK"
```
Expected: `wheel cloud/ excluded: OK`

- [ ] **Step 4: Commit**

```bash
git add LICENSE pyproject.toml
git commit -m "chore: dual-license (Apache-2.0 core, proprietary cloud) + exclude cloud from build"
```

---

## Task 10: End-to-end verification + full test suite

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `pytest tests/ -q`
Expected: all tests pass (367 existing + new).

- [ ] **Step 2: Lint + type check**

Run: `ruff check src/ && mypy src/argus`
Expected: no errors.

- [ ] **Step 3: Confirm zero Supabase URL references in the core**

Run: `rg "isnphpbckxfjsxllryrg" src/argus/ && echo "LEAK" || echo "clean: no supabase url in src/argus"`
Expected: `clean: no supabase url in src/argus`

- [ ] **Step 4: Fresh-venv BYOK smoke test**

```bash
python -m venv /tmp/argus-byok && /tmp/argus-byok/bin/pip install -e ".[all]"
/tmp/argus-byok/bin/argus key set sk-fake-for-doctor
/tmp/argus-byok/bin/argus doctor
```
Expected: `doctor` shows `llm  BYOK (saved) — calling OpenAI directly`, storage healthy, no "please login" errors, no calls to ARGUS Supabase.

- [ ] **Step 5: Confirm heuristic-only path (no key, no login)**

```bash
/tmp/argus-byok/bin/argus key clear
env -u OPENAI_API_KEY /tmp/argus-byok/bin/argus doctor
```
Expected: `llm  heuristic-only — no key set`, all other checks pass, no crash.

- [ ] **Step 6: Final commit (if any verification fixups were needed)**

```bash
git add -A
git commit -m "test: verify BYOK open-core path end-to-end" --allow-empty
```

---

## Notes for the implementer

- **No new runtime dependency.** `llm_proxy.py` uses stdlib `urllib` for the direct OpenAI call, matching the existing proxy call style. `openai` stays an optional dep, used only by `embedding_store`.
- **Callers are unchanged.** All 11 modules that call `create_chat_completion`/`is_available` already handle the `{"error": ...}` return, so heuristic fallback is automatic.
- **`cloud/` is git-tracked but PyPI-excluded** — this is intentional open-core, not a secret. The anon key it contains is RLS-protected and public-safe.
- **Hosted deployment** must call `cloud.config.apply_env()` (or otherwise set `ARGUS_SUPABASE_URL` / `ARGUS_SUPABASE_ANON_KEY`) before importing `argus.cloud` for proxy/sync to activate.
