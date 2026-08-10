"""Supabase REST client for ARGUS cloud sync.

Uses only stdlib (urllib) to avoid adding dependencies.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Supabase config (hosted deployments set these via cloud/config.py) ────
SUPABASE_URL = os.environ.get("ARGUS_SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("ARGUS_SUPABASE_ANON_KEY")

_CREDENTIALS_DIR = Path.home() / ".argus"
_CREDENTIALS_FILE = _CREDENTIALS_DIR / "credentials.json"


# ── Credentials ──────────────────────────────────────────────────────────


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    user_id: str
    email: str
    expires_at: float  # unix timestamp


def save_credentials(creds: Credentials) -> None:
    _CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    _CREDENTIALS_FILE.write_text(
        json.dumps(
            {
                "access_token": creds.access_token,
                "refresh_token": creds.refresh_token,
                "user_id": creds.user_id,
                "email": creds.email,
                "expires_at": creds.expires_at,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _CREDENTIALS_FILE.chmod(0o600)


def load_credentials() -> Credentials | None:
    if not _CREDENTIALS_FILE.exists():
        return None
    try:
        data = json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
        return Credentials(**data)
    except Exception:
        return None


def clear_credentials() -> None:
    if _CREDENTIALS_FILE.exists():
        _CREDENTIALS_FILE.unlink()


def is_logged_in() -> bool:
    if SUPABASE_URL is None:
        return False
    creds = load_credentials()
    return creds is not None


# ── Token refresh ────────────────────────────────────────────────────────


def _refresh_if_needed(creds: Credentials) -> Credentials | None:
    """Refresh the access token if it expires within 60 seconds."""
    if time.time() < creds.expires_at - 60:
        return creds

    try:
        if SUPABASE_ANON_KEY is None:
            return None
        body = json.dumps({"refresh_token": creds.refresh_token}).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
            data=body,
            headers={
                "Content-Type": "application/json",
                "apikey": SUPABASE_ANON_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        creds = Credentials(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            user_id=data["user"]["id"],
            email=data["user"].get("email", creds.email),
            expires_at=time.time() + data.get("expires_in", 3600),
        )
        save_credentials(creds)
        return creds
    except Exception:
        return None


def _get_valid_credentials() -> Credentials | None:
    if SUPABASE_URL is None:
        return None
    creds = load_credentials()
    if creds is None:
        return None
    return _refresh_if_needed(creds)


# ── REST helpers ─────────────────────────────────────────────────────────


def _supabase_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    access_token: str,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """Make an authenticated request to the Supabase REST API."""
    if SUPABASE_ANON_KEY is None:
        raise RuntimeError("ARGUS_SUPABASE_ANON_KEY not configured")
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw)


# ── Public API ───────────────────────────────────────────────────────────


def push_run(run_data: dict[str, Any]) -> bool:
    """Push a run record to Supabase. Returns True on success."""
    creds = _get_valid_credentials()
    if creds is None:
        return False

    row = {
        "user_id": creds.user_id,
        "run_id": run_data["run_id"],
        "data": run_data,
        "overall_status": run_data.get("overall_status"),
        "started_at": run_data.get("started_at"),
        "duration_ms": (
            int(run_data["duration_ms"]) if run_data.get("duration_ms") is not None else None
        ),
        "step_count": len(run_data.get("steps", [])),
        "first_failure_step": run_data.get("first_failure_step"),
        "argus_version": run_data.get("argus_version"),
        "parent_run_id": run_data.get("parent_run_id"),
    }

    try:
        _supabase_request(
            "POST",
            "runs",
            body=row,
            access_token=creds.access_token,
            extra_headers={
                "Prefer": "resolution=merge-duplicates",
            },
        )
        return True
    except Exception:
        return False


def push_run_async(run_data: dict[str, Any]) -> None:
    """Push a run in a background thread (fire-and-forget)."""
    thread = threading.Thread(target=push_run, args=(run_data,), daemon=True)
    thread.start()


# ── Shared Signatures ───────────────────────────────────────────────────


def push_shared_signature(sig_data: dict[str, Any]) -> bool:
    """Push an approved signature to the shared community table.

    Returns True on success, False if not logged in or on error.
    """
    creds = _get_valid_credentials()
    if creds is None:
        return False

    row = {
        "user_id": creds.user_id,
        "sig_id": sig_data["id"],
        "category": sig_data["category"],
        "pattern": sig_data["pattern"],
        "match_strategy": sig_data["match_strategy"],
        "severity": sig_data["severity"],
        "description": sig_data["description"],
        "reasoning": sig_data.get("reasoning"),
        "evidence": sig_data.get("evidence", []),
        "confidence": sig_data.get("metadata", {}).get("confidence"),
        "source_run_ids": sig_data.get("source_run_ids", []),
        "source_nodes": sig_data.get("source_nodes", []),
        "times_seen": sig_data.get("metadata", {}).get("frequency", 1),
        "contributed_by": creds.email,
    }

    try:
        _supabase_request(
            "POST",
            "shared_signatures",
            body=row,
            access_token=creds.access_token,
            extra_headers={"Prefer": "resolution=merge-duplicates"},
        )
        return True
    except Exception:
        return False


def pull_shared_signatures() -> list[dict[str, Any]]:
    """Pull all shared signatures from Supabase.

    Returns a list of signature dicts ready for the registry, or an
    empty list if not logged in or on error.
    """
    creds = _get_valid_credentials()
    if creds is None:
        return []

    try:
        rows = _supabase_request(
            "GET",
            "shared_signatures?select=*&order=created_at.desc",
            access_token=creds.access_token,
        )
        if not isinstance(rows, list):
            return []

        sigs: list[dict[str, Any]] = []
        for row in rows:
            sigs.append(
                {
                    "id": row["sig_id"],
                    "category": row["category"],
                    "pattern": row["pattern"],
                    "match_strategy": row["match_strategy"],
                    "severity": row["severity"],
                    "description": row["description"],
                    "source": "shared",
                    "metadata": {
                        "confidence": row.get("confidence"),
                        "frequency": row.get("times_seen", 1),
                        "approval_status": "approved",
                        "contributed_by": row.get("contributed_by"),
                        "framework_specific": None,
                    },
                }
            )
        return sigs
    except Exception:
        return []


def pull_shared_signatures_async(
    callback: Any = None,
) -> threading.Thread:
    """Pull shared signatures in a background thread.

    If callback is provided, it is called with the result list.
    """

    def _run() -> None:
        result = pull_shared_signatures()
        if callback is not None:
            callback(result)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
