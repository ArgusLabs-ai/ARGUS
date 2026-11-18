"""``argus cloud pull-baselines``: the baselines a team workspace learned from main, on disk.

The workspace answers ``GET /api/v1/baselines`` (same OIDC token as the upload; the token says
which repository) with ``{"schema": 1, "agents": {<agent>: {"parts": [<baseline>, ...]}}}``:
the last green main runs' baseline parts. Each agent's parts are merged by the OSS code
(``trace_rules.merge_baselines``; the server holds no detection logic) into
``.argus/baselines/<agent>.json``, which the recorder loads when it is given no baseline.

Nothing learned yet (a 404, or an agent with no parts) is "learning", not an error. An agent
whose name is not a plain file name, or with any part that is not a baseline, gets no file:
merging only some of the parts would claim more than the runs agreed on.

The answer also carries the repository's payload mode (``"payloads": "shapes" | "full"``). Only
``"full"`` writes ``.argus/cloud_payloads``, which tells ``argus cloud upload`` to send each run
file beside its card. The file is removed first on every pull, so anything else (no answer, a
404, a failed pull, a missing or unknown mode) leaves the project on shapes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from argus import __version__, storage
from argus.cloud_upload import (
    UploadError,
    _oidc_request,
    _oidc_token,
    _send,
    _workspace,
)
from argus.trace_rules import is_baseline, merge_baselines

SCHEMA = 1
PULL_TIMEOUT_S = 30.0
MAX_BASELINES_BYTES = 16 * 1024 * 1024
_VERB = "pull baselines"


@dataclass(frozen=True)
class Pulled:
    written: int = 0
    learning: int = 0
    skipped: int = 0
    full_payloads: bool = False


def pull(*, environ: Mapping[str, str] | None = None) -> Pulled:
    """Fetch and write every agent's baseline; ``UploadError`` (one line) on any failure."""
    env = os.environ if environ is None else environ
    # Off first, before anything can fail: only an answer from this job's pull turns it on, so a
    # file left on a reused runner, or a pull that failed, never sends values.
    storage.payloads_path().unlink(missing_ok=True)
    request_url, request_token = _oidc_request(env, _VERB)
    endpoint, host = _workspace(env, "/api/v1/baselines", _VERB)
    token = _oidc_token(request_url, request_token, _VERB)
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"argus-agents/{__version__}",
        },
    )
    raw = _send(
        request, PULL_TIMEOUT_S, host, read=MAX_BASELINES_BYTES + 1, verb=_VERB, missing_ok=True
    )
    if raw is None:
        return Pulled()
    body = _body(raw, host)
    pulled = _write(body["agents"])
    if body.get("payloads") == "full":
        path = storage.payloads_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("full\n", encoding="utf-8")
        return dataclasses.replace(pulled, full_payloads=True)
    return pulled


def _body(raw: bytes, host: str) -> dict[str, Any]:
    if len(raw) > MAX_BASELINES_BYTES:
        raise UploadError(f"pull baselines failed: the answer from {host} is too large")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # nested too deep to parse
        body = None
    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        raise UploadError(f"pull baselines failed: {host} did not answer with baselines")
    agents = body.get("agents")
    if not isinstance(agents, dict):
        raise UploadError(f"pull baselines failed: {host} did not answer with baselines")
    return body


def _write(agents: dict[Any, Any]) -> Pulled:
    written = learning = skipped = 0
    for agent, entry in agents.items():
        path = storage.baseline_path(agent)
        parts = entry.get("parts") if isinstance(entry, dict) else None
        if path is None or not isinstance(parts, list) or not all(map(is_baseline, parts)):
            skipped += 1
        elif not parts:
            learning += 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(merge_baselines(parts), indent=2) + "\n"
            # A fresh temp file beside the target (never one already there, which could be a
            # symlink), then an atomic rename over it.
            fd, partial = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".partial"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(partial, path)
            written += 1
    return Pulled(written, learning, skipped)
