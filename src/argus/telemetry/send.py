"""Send one shape card per graded run. On by default; see docs/DATA.md.

Off switches, strongest first: ``DO_NOT_TRACK=1``, ``ARGUS_TELEMETRY=0|1``,
``argus telemetry off|on`` (saved in ``~/.argus/telemetry.json``).

Every card is written to ``<project>/.argus/outbox/`` before it is sent, so a
user can read exactly what left the machine. Identical cards (same graph,
verdict and findings) go once per day, and at most ``DAILY_CAP`` a day go at
all. Nothing here may raise into a user's run: ``storage.save_run`` calls this
inside a ``try``, and the POST swallows every error.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable

from argus.models import RunRecord
from argus.storage import argus_dir
from argus.telemetry.card import build_card

SETTINGS_FILE = Path.home() / ".argus" / "telemetry.json"
# The telemetry-intake edge function (supabase/telemetry/). Override for testing.
INTAKE_URL = "https://hpzvhxodoqlsvbodpxcr.supabase.co/functions/v1/telemetry-intake"
ENDPOINT: str | None = os.environ.get("ARGUS_TELEMETRY_URL") or INTAKE_URL
DAILY_CAP = 200
OUTBOX_KEEP = 500
TIMEOUT_S = 2.0

_OFF = {"0", "false", "off", "no"}
_ON = {"1", "true", "on", "yes"}
_CI_VARS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "CIRCLECI",
    "JENKINS_URL",
    "TF_BUILD",
)
_DATA_URL = "https://github.com/ArgusLabs-ai/ARGUS/blob/master/docs/DATA.md"
_NOTICE = (
    "argus: sending anonymous run shapes (no values, no names) to improve detection.\n"
    f"       What is sent: {_DATA_URL}\n"
    "       Turn off: argus telemetry off  (or ARGUS_TELEMETRY=0 / DO_NOT_TRACK=1)"
)


# ── settings ────────────────────────────────────────────────────────────────


def _read() -> dict[str, Any]:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict[str, Any]) -> None:
    # ponytail: last writer wins across concurrent processes; worst case a
    # card more or less than the cap. Add a file lock if that ever matters.
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(SETTINGS_FILE)


def _settings() -> dict[str, Any]:
    """Saved settings, with an install id and hashing secret made on first use."""
    data = _read()
    if data.get("install_id") and data.get("secret"):
        return data
    created = {
        **data,
        "install_id": "i_" + secrets.token_hex(8),
        "secret": secrets.token_hex(32),
    }
    _write(created)
    return created


def set_enabled(enabled: bool) -> None:
    _write({**_settings(), "enabled": enabled})


def status() -> tuple[bool, str]:
    """(on?, which setting decided it)."""
    if os.environ.get("DO_NOT_TRACK", "").strip().lower() in _ON:
        return False, "DO_NOT_TRACK"
    env = os.environ.get("ARGUS_TELEMETRY", "").strip().lower()
    if env in _OFF:
        return False, "ARGUS_TELEMETRY"
    if env in _ON:
        return True, "ARGUS_TELEMETRY"
    saved = _read().get("enabled")
    if saved is False:
        return False, "argus telemetry off"
    if saved is True:
        return True, "argus telemetry on"
    return True, "default"


def is_ci() -> bool:
    return any(
        os.environ.get(var, "").strip().lower() not in ("", "0", "false") for var in _CI_VARS
    )


# ── outbox ──────────────────────────────────────────────────────────────────


def outbox_dir() -> Path:
    return argus_dir() / "outbox"


def _to_outbox(card: dict[str, Any]) -> Path:
    box = outbox_dir()
    box.mkdir(parents=True, exist_ok=True)
    path = box / f"{card['run']}.json"
    path.write_text(json.dumps(card, indent=2), encoding="utf-8")
    old = sorted(box.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-OUTBOX_KEEP]
    for stale in old:
        stale.unlink(missing_ok=True)
    return path


def last_card() -> Path | None:
    box = outbox_dir()
    cards = sorted(box.glob("*.json"), key=lambda p: p.stat().st_mtime) if box.is_dir() else []
    return cards[-1] if cards else None


# ── sending ─────────────────────────────────────────────────────────────────


def _today() -> str:
    return date.today().isoformat()


def _dedupe_key(card: dict[str, Any]) -> str:
    findings = sorted((f["type"], f["node"], f["severity"]) for f in card["findings"])
    raw = json.dumps([card["fingerprint"], card["status"], findings])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _post(url: str, card: dict[str, Any]) -> None:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(card).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT_S).close()
    except Exception:
        pass  # telemetry is best-effort; a dropped card is fine, a broken run is not


def _start(fn: Callable[..., None], *args: Any) -> None:
    # Not a daemon: a short script would exit before the POST. TIMEOUT_S bounds
    # how long interpreter shutdown can wait on it.
    threading.Thread(target=fn, args=args, daemon=False).start()


def send_run(record: RunRecord) -> Path | None:
    """Build, save and send this run's card. Returns the outbox path, or None if not sent."""
    if ENDPOINT is None or not status()[0]:
        return None
    settings = _settings()
    card = build_card(
        record, secret=settings["secret"], install_id=settings["install_id"], ci=is_ci()
    )
    today = _today()
    same_day = settings.get("day") == today
    sent = settings.get("sent", 0) if same_day else 0
    seen = settings.get("seen", []) if same_day else []
    key = _dedupe_key(card)
    if key in seen or sent >= DAILY_CAP:
        return None

    path = _to_outbox(card)
    if not settings.get("notice_shown"):
        print(_NOTICE, file=sys.stderr)  # noqa: T201
    _write(
        {
            **settings,
            "day": today,
            "sent": sent + 1,
            "seen": [*seen, key][-DAILY_CAP:],
            "notice_shown": True,
        }
    )
    _start(_post, ENDPOINT, card)
    return path
