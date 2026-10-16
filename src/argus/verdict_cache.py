"""Authenticated, content-addressed disk cache for LLM judge verdicts.

The two LLM judges that rule on a run (the per-step coherence judge and the run
reviewer) pay a full LLM call for a verdict even when they are asked the exact same
question they answered minutes ago: a replay, a re-run of CI. This cache answers the
repeat from disk.

Reviewer-not-gate: the cache stores what a judge *said*, never a gate decision. A
served verdict goes through the same code as a fresh reply, and is marked
``cached=True`` in its metadata.

Only a request that can claim to be reproducible is ever read from or written to the
cache (``determinism.gate``: temperature 0.0, a seed, a provider that honors it), so with
the defaults (no seed) the cache is never touched.

Public surface (everything else here is private)
------------------------------------------------
``request_key(request, route=...) -> content_hash``
    The content address. ``request`` is the exact keyword dict handed to
    ``create_chat_completion`` AFTER the prompt was assembled (model, messages,
    tools, tool_choice, response_format, temperature, max_tokens and the seed);
    ``timeout`` and ``api_key`` are transport and ignored. A parameter that is
    ``None`` is dropped, so a request that does not carry a parameter hashes the same
    whether the key is absent or ``None``. ``route`` is the provider and the model the
    request really maps to (see ``llm_proxy.judge_route``).
``current_scope(salt=None) -> CacheScope | None``
    Where (the project's ``.argus/verdict_cache.db``) and under which salt this
    process caches. ``None`` when the cache is switched off
    (``ARGUS_VERDICT_CACHE=0``). Judges take it as ``cache=``; ``None`` means
    "do not cache", so a judge called without a scope never touches the disk.
``lookup(scope, content_hash, model) -> dict | None``
``store(scope, content_hash, model, verdict) -> bool``
    Never raise. A missing, corrupt, forged or foreign row is a miss; a cache that
    cannot work at all (unusable install key, unwritable database) logs ONE warning and
    then acts as a permanent miss.
``resolve_salt(explicit=None) -> str``
    ``explicit`` > ``$ARGUS_RUN_SALT`` > a hash of the project root. Never the run id:
    a replay or a CI re-run of the same content must find the row.
``install_key_path()`` / ``ARGUS_VERDICT_KEY_FILE``
    Where the per-install key lives (below).

Security model
--------------
What this guards is *forgery*, not secrecy. A cached verdict is trusted the way a fresh
one is (a stored ``{"failures": []}`` would silence the reviewer), so a row nobody but this
install could have written must be the only kind served. Rows are keyed
``(content_hash, model, key_salt)`` and carry an HMAC-SHA256 over that row context and the
verdict, under an *install key*: 32 random bytes generated once per install and stored
OUTSIDE the project (``~/.argus/verdict_cache.key`` next to the user config, mode 0600,
directory 0700 when created; ``$ARGUS_VERDICT_KEY_FILE`` overrides the path, e.g. a restored
CI secret). The row context is part of the MAC, so a verdict cannot be moved to another row.
Without the install key a row can be neither forged nor altered, and a row that fails
authentication is a miss.

The verdict itself is stored as readable JSON: it is a judge's reply about data that already
sits, unencrypted, in ``.argus/runs/`` next to it, so sealing it would protect nothing the run
files leak. The database file is created 0600 (directory 0700 when created). Rows are pruned by
age and count on every store (``MAX_AGE_DAYS``, ``MAX_ROWS``); a row past the age limit is also
a miss on lookup, without waiting for a store to prune it.

Standard library only; this module imports no LLM client code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("argus")

CACHE_FILE = "verdict_cache.db"  # under <project-root>/.argus/
KEY_FILE = "verdict_cache.key"  # under ~/.argus/, next to the user config
MAX_ROWS = 5000
MAX_AGE_DAYS = 90

# How long a judge may wait on a locked database before giving up on the cache.
_BUSY_S = 1.0
_KEY_BYTES = 32
_FORMAT = "argus-verdict-v1"
# Transport, not content: they cannot change what the model is asked.
_TRANSPORT_ONLY = frozenset({"timeout", "api_key"})

_warned: set[str] = set()
_warned_lock = threading.Lock()


class _Disabled(Exception):
    """The cache cannot work in this environment; ``code`` names why (warned once)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _warn_once(code: str, message: str) -> None:
    with _warned_lock:
        if code in _warned:
            return
        _warned.add(code)
    logger.warning("argus verdict cache: %s", message)


# ── Content address, salt, scope ─────────────────────────────────────────────


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, tight separators, ``str`` for odd values."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def request_key(request: Mapping[str, Any], *, route: str = "") -> str:
    """Content address of one judge request: sha256 of everything the model is asked.

    Hash the same dict you send (``create_chat_completion(**request, timeout=...)``)
    so the key and the call cannot drift apart. See the module docstring.
    """
    body = {k: v for k, v in request.items() if k not in _TRANSPORT_ONLY and v is not None}
    return hashlib.sha256(
        canonical_json({"format": _FORMAT, "route": route, "request": body}).encode("utf-8")
    ).hexdigest()


def resolve_salt(explicit: str | None = None, *, root: Path | None = None) -> str:
    """Namespace for cache rows: ``explicit``, else ``$ARGUS_RUN_SALT``, else the project.

    The default is a hash of the project root, never a run id, so two runs (a
    replay, a CI re-run) of identical content share rows. Set ``ARGUS_RUN_SALT``
    to keep, say, CI and local verdicts apart in one database.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("ARGUS_RUN_SALT", "").strip()
    if env:
        return env
    if root is None:
        from argus.storage import resolve_project_root

        root = resolve_project_root()
    return "project:" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CacheScope:
    """Where and under which salt a judge caches. Captured once, used from any thread."""

    db_path: Path
    salt: str


def current_scope(salt: str | None = None) -> CacheScope | None:
    """The cache scope for the project this process is in, or None (cache off).

    Capture it where the judge is *submitted* (the main thread) and hand it to
    the judge: cwd and environment are process-wide and can move before a pool
    thread runs. Never raises.
    """
    if os.environ.get("ARGUS_VERDICT_CACHE", "").strip().lower() in {"0", "false", "no", "off"}:
        return None
    try:
        from argus.storage import argus_dir

        db_dir = argus_dir()
        return CacheScope(db_path=db_dir / CACHE_FILE, salt=resolve_salt(salt, root=db_dir.parent))
    except Exception as exc:  # e.g. the working directory was deleted under us
        _warn_once("scope", f"disabled, cannot locate the project ({exc})")
        return None


# ── Install key ──────────────────────────────────────────────────────────────


def install_key_path() -> Path:
    """Where this install's key lives: outside ``.argus/``, next to the user config."""
    override = os.environ.get("ARGUS_VERDICT_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    from argus import user_config

    return user_config._CONFIG_DIR / KEY_FILE


def _read_key(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Disabled("key", f"disabled, install key {path} is unreadable ({exc})") from exc
    if len(data) != _KEY_BYTES:
        raise _Disabled(
            "key",
            f"disabled, install key {path} is not a {_KEY_BYTES}-byte key "
            "(delete it to generate a new one; cached verdicts will be re-asked)",
        )
    return data


def _create_key(path: Path) -> bytes:
    """Generate the install key, 0600, race-safe: two first runs end up sharing one key."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(os.urandom(_KEY_BYTES))
            try:
                os.link(tmp, path)  # atomic; loses cleanly to a concurrent creator
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        raise _Disabled("key", f"disabled, cannot create the install key {path} ({exc})") from exc
    key = _read_key(path)
    if key is None:
        raise _Disabled("key", f"disabled, the install key {path} vanished after creation")
    return key


def _install_key(*, create: bool) -> bytes | None:
    path = install_key_path()
    key = _read_key(path)
    if key is None and create:
        key = _create_key(path)
    return key


# ── Row authentication ───────────────────────────────────────────────────────


def _row_context(content_hash: str, model: str, salt: str) -> bytes:
    return json.dumps([_FORMAT, content_hash, model, salt], separators=(",", ":")).encode("utf-8")


def _mac(install_key: bytes, context: bytes, verdict_json: str) -> str:
    """HMAC-SHA256 over the row context and the verdict text exactly as stored."""
    message = context + b"\x00" + verdict_json.encode("utf-8")
    return hmac.new(install_key, message, hashlib.sha256).hexdigest()


# ── Storage ──────────────────────────────────────────────────────────────────

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS verdicts ("
    "content_hash TEXT NOT NULL, model TEXT NOT NULL, key_salt TEXT NOT NULL, "
    "verdict TEXT NOT NULL, mac TEXT NOT NULL, created_at TEXT NOT NULL, "
    "PRIMARY KEY (content_hash, model, key_salt))"
)


def _open(db_path: Path, *, create: bool) -> sqlite3.Connection | None:
    """A short-lived connection (callers close it): no pooled handle to leak or go stale."""
    if not db_path.exists():
        if not create:
            return None
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(db_path, os.O_RDWR | os.O_CREAT, 0o600))
    return sqlite3.connect(str(db_path), timeout=_BUSY_S)


def _age_cutoff(now: datetime) -> str:
    """Rows stamped before this are too old: pruned on a store, a miss on a lookup."""
    return (now - timedelta(days=MAX_AGE_DAYS)).isoformat(timespec="microseconds")


def _prune(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute("DELETE FROM verdicts WHERE created_at < ?", (_age_cutoff(now),))
    conn.execute(
        "DELETE FROM verdicts WHERE rowid IN "
        "(SELECT rowid FROM verdicts ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
        (MAX_ROWS,),
    )


def _lookup(scope: CacheScope, content_hash: str, model: str) -> dict[str, Any] | None:
    install_key = _install_key(create=False)
    if install_key is None:
        return None  # no key yet, so no row of this install exists
    conn = _open(scope.db_path, create=False)
    if conn is None:
        return None
    with closing(conn):
        try:
            row = conn.execute(
                "SELECT verdict, mac FROM verdicts "
                "WHERE content_hash = ? AND model = ? AND key_salt = ? AND created_at >= ?",
                (content_hash, model, scope.salt, _age_cutoff(datetime.now(timezone.utc))),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
    if row is None:
        return None
    verdict_json, mac = row
    context = _row_context(content_hash, model, scope.salt)
    try:
        authentic = hmac.compare_digest(_mac(install_key, context, verdict_json), mac)
        verdict = json.loads(verdict_json) if authentic else None
    except (ValueError, TypeError, AttributeError):
        return None  # corrupt: a miss, never a hit
    return verdict if isinstance(verdict, dict) else None  # forged or foreign rows are misses


def _store(scope: CacheScope, content_hash: str, model: str, verdict: dict[str, Any]) -> None:
    install_key = _install_key(create=True)
    assert install_key is not None  # create=True returns a key or raises
    context = _row_context(content_hash, model, scope.salt)
    verdict_json = canonical_json(verdict)
    mac = _mac(install_key, context, verdict_json)
    now = datetime.now(timezone.utc)
    conn = _open(scope.db_path, create=True)
    assert conn is not None
    with closing(conn), conn:  # `with conn` commits (or rolls back); closing() releases the file
        conn.execute(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO verdicts "
            "(content_hash, model, key_salt, verdict, mac, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                content_hash,
                model,
                scope.salt,
                verdict_json,
                mac,
                now.isoformat(timespec="microseconds"),
            ),
        )
        _prune(conn, now)


def lookup(scope: CacheScope | None, content_hash: str, model: str) -> dict[str, Any] | None:
    """The cached verdict payload for this request, or None. Never raises."""
    if scope is None:
        return None
    try:
        return _lookup(scope, content_hash, model)
    except _Disabled as exc:
        _warn_once(exc.code, str(exc))
    except Exception as exc:  # a cache is an optimization: it must never cost a verdict
        _warn_once("db", f"unavailable ({type(exc).__name__}: {exc}); judging live")
    return None


def store(
    scope: CacheScope | None, content_hash: str, model: str, verdict: dict[str, Any]
) -> bool:
    """Write one authenticated verdict payload; True when it was written. Never raises."""
    if scope is None:
        return False
    try:
        _store(scope, content_hash, model, verdict)
        return True
    except _Disabled as exc:
        _warn_once(exc.code, str(exc))
    except Exception as exc:  # a full disk or a locked file must never fail a judge
        _warn_once("db", f"unavailable ({type(exc).__name__}: {exc}); judging live")
    return False
