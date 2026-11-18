"""``argus cloud upload``: send this job's run cards to a team workspace.

What goes up is the run card (``argus.cloud_card``): the verdict, the findings and the shape of
the data, never a value (docs/DATA.md). The cards are those of the runs the pytest session
saved (``.argus/session_runs.txt``, written by ``pytest --argus-report-only`` / ``--argus``).

The job proves who it is with the OIDC token GitHub Actions hands it, so there is no secret to
set up and nothing long-lived to leak. The workspace reads the repository, ref and commit from
that token; the body names none of them. The token is only available when the job has
``permissions: id-token: write``, which is why this runs only in GitHub Actions for now.

It fails loudly. Anything but a 2xx answer, a timeout, an unreachable host or a missing
precondition raises ``UploadError``; the CLI prints its one line and exits 1. (The opt-in
full-run sync it replaces swallowed its errors, so a job that could not report looked like one
that had.) A line never carries a card, a response body or a token, and a redirect is not
followed, so the token cannot be passed on to another host.

A project whose workspace turned on full payloads (``.argus/cloud_payloads`` says ``full``,
written by ``argus cloud pull-baselines`` from the workspace's answer) also sends each run file,
as saved on the runner, in ``payloads`` beside the cards: ``{run_id: <run file>}``. The card is
the same either way. Then the cards go in as many posts as keep each body under
``MAX_BODY_BYTES`` (only the last carries ``final``), and a run file too large for any post is left
out: its card still goes. Without that file nothing but cards is ever sent.

Standard library only: ``urllib`` with a timeout.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from argus import __version__, run_context, storage
from argus.cloud_card import build_card

ENVELOPE_SCHEMA = 1
AUDIENCE = "argus-cloud"
TOKEN_TIMEOUT_S = 10.0
UPLOAD_TIMEOUT_S = 30.0
MAX_TOKEN_BYTES = 64 * 1024  # a JWT is a few KB
# Under the workspace's 4 MB cap, with room for the envelope. Only full payloads come near it.
MAX_BODY_BYTES = 3_500_000
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_OIDC = "the GitHub OIDC endpoint"


class UploadError(Exception):
    """Why the upload (or ``pull-baselines``, which shares these requests) did not happen.
    The message is one line and carries no card content, response body or token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error. urllib would repeat the request, ``Authorization`` header included,
    at whatever host the answer names."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def audience_url(request_url: str) -> str:
    """GitHub's token request URL already has a query string; add the audience to it."""
    return f"{request_url}{'&' if '?' in request_url else '?'}audience={AUDIENCE}"


@dataclass(frozen=True)
class Uploaded:
    cards: int = 0
    payloads: int = 0  # run files sent beside their cards (full payloads only)
    too_large: int = 0  # run files left out because no post could hold them


def upload(*, final: bool, environ: Mapping[str, str] | None = None) -> Uploaded:
    """Send the session's cards. ``final`` says no more will follow.

    Cheap checks come first, the OIDC token last: it expires, and building cards can take a
    while. With no runs it still posts (an empty list): the workspace must learn the job ended.
    """
    env = os.environ if environ is None else environ
    request_url, request_token = _oidc_request(env)
    endpoint, host = _workspace(env)
    cards, payloads = _cards(baseline_part=on_default_branch(env), full=full_payloads())
    batches, too_large = _batches(cards, payloads)
    token = _oidc_token(request_url, request_token)
    for i, (batch, files) in enumerate(batches):
        envelope: dict[str, Any] = {
            "schema": ENVELOPE_SCHEMA,
            "final": final and i == len(batches) - 1,
            "cards": batch,
        }
        if files:
            envelope["payloads"] = files
        request = urllib.request.Request(
            endpoint,
            data=_encode(envelope),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"argus-agents/{__version__}",
            },
        )
        _send(request, UPLOAD_TIMEOUT_S, host, read=0)
    sent = sum(len(files) for _, files in batches)
    return Uploaded(len(cards), sent, too_large)


def full_payloads() -> bool:
    """Did this job's ``pull-baselines`` say the project opted into full payloads? Anything but a
    readable file saying exactly ``full`` is no."""
    try:
        return storage.payloads_path().read_text(encoding="utf-8").strip() == "full"
    except (OSError, UnicodeDecodeError):
        return False


def _encode(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


_Batch = tuple[list[dict[str, Any]], dict[str, Any]]


def _batches(cards: list[dict[str, Any]], payloads: dict[str, Any]) -> tuple[list[_Batch], int]:
    """Cards (and their run files) packed in order into posts that stay under the cap. Without
    payloads that is one post, exactly as before. A run file that cannot fit even alone beside
    its card is dropped (the card still goes); a card alone is far under the cap."""
    if not payloads:
        return [(cards, {})], 0
    envelope = len(
        _encode({"schema": ENVELOPE_SCHEMA, "final": False, "cards": [], "payloads": {}})
    )
    batches: list[_Batch] = [([], {})]
    size, too_large = envelope, 0
    for card in cards:
        run_id = card["run"]["run_id"]
        need = len(_encode(card)) + 1
        extra = len(_encode({run_id: payloads[run_id]})) if run_id in payloads else 0
        if extra and envelope + need + extra > MAX_BODY_BYTES:
            extra, too_large = 0, too_large + 1
        if size + need + extra > MAX_BODY_BYTES and batches[-1][0]:
            batches.append(([], {}))
            size = envelope
        batches[-1][0].append(card)
        if extra:
            batches[-1][1][run_id] = payloads[run_id]
        size += need + extra
    return batches, too_large


# ── preconditions ────────────────────────────────────────────────────────────


def _oidc_request(env: Mapping[str, str], verb: str = "upload") -> tuple[str, str]:
    url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not url or not token:
        raise UploadError(
            f"cannot {verb}: no GitHub OIDC token is available. This runs in GitHub Actions, "
            "with 'permissions: id-token: write' on the job (a fork's pull request gets none)"
        )
    _checked(url, "ACTIONS_ID_TOKEN_REQUEST_URL", verb)
    return url, token


def _workspace(
    env: Mapping[str, str], path: str = "/api/v1/runs", verb: str = "upload"
) -> tuple[str, str]:
    """The workspace endpoint at ``path`` and the host name to show in a message."""
    raw = env.get("ARGUS_CLOUD_URL", "").strip()
    if not raw:
        raise UploadError(f"cannot {verb}: ARGUS_CLOUD_URL is not set")
    parts = _checked(raw, "ARGUS_CLOUD_URL", verb)
    return f"{raw.rstrip('/')}{path}", parts.hostname or ""


def _checked(raw: str, name: str, verb: str = "upload") -> urllib.parse.SplitResult:
    """A URL a bearer token may be sent to: https, or http on this machine (a local fake)."""
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
    except ValueError:
        raise UploadError(f"cannot {verb}: {name} is not a usable URL") from None
    if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOOPBACK):
        raise UploadError(f"cannot {verb}: {name} must be an https URL")
    if not host or parts.username or parts.password:
        raise UploadError(f"cannot {verb}: {name} is not a usable URL")
    return parts


def _cards(
    *, baseline_part: bool, full: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The card of every run the session saved, and with ``full`` each run file by run id. One
    that cannot be read or built stops the upload: a workspace that judges half a job would
    judge it wrong."""
    path = storage.session_path()
    run_ids = run_context.session_run_ids(path)
    if run_ids is None:
        raise UploadError(
            f"cannot upload: no record of this job's runs ({path.name}). "
            "Run pytest with --argus-report-only first"
        )
    cards: list[dict[str, Any]] = []
    payloads: dict[str, Any] = {}
    for run_id in run_ids:
        shown = _printable(run_id)
        try:
            record = storage.load_run(run_id)
            if record.run_id != run_id:  # load_run also takes a prefix; a list holds whole ids
                raise LookupError(run_id)
        except Exception as exc:  # noqa: BLE001 - named by class only: a message can quote a value
            raise UploadError(
                f"cannot upload: run {shown} is in the session list but cannot be read "
                f"({type(exc).__name__}); nothing was sent"
            ) from None
        try:
            cards.append(build_card(record, baseline_part=baseline_part))
            if full:
                # The file as saved (redact_keys already applied), not a re-serialization.
                payloads[run_id] = json.loads(storage.load_run_text(run_id))
        except Exception as exc:  # noqa: BLE001
            raise UploadError(
                f"cannot upload: could not build the card for run {shown} "
                f"({type(exc).__name__}); nothing was sent"
            ) from None
    return cards, payloads


def on_default_branch(env: Mapping[str, str]) -> bool:
    """Is this job a push to the repository's default branch? Only then may a clean run's card
    carry a baseline part. The default branch is read from GitHub's event file; anything that
    cannot be read means no (a part is a bonus, never worth failing an upload for). The
    workspace checks the ref again from the token."""
    if env.get("GITHUB_EVENT_NAME") != "push":
        return False
    try:
        with open(env.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as f:
            branch = json.load(f)["repository"]["default_branch"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (
        isinstance(branch, str)
        and bool(branch)
        and env.get("GITHUB_REF") == f"refs/heads/{branch}"
    )


# ── the two requests ─────────────────────────────────────────────────────────


def _oidc_token(request_url: str, request_token: str, verb: str = "upload") -> str:
    request = urllib.request.Request(
        audience_url(request_url),
        headers={
            "Authorization": f"bearer {request_token}",
            "Accept": "application/json; api-version=2.0",
            "User-Agent": f"argus-agents/{__version__}",
        },
    )
    answer = _send(request, TOKEN_TIMEOUT_S, _OIDC, read=MAX_TOKEN_BYTES, verb=verb)
    try:
        value = json.loads(answer or b"").get("value")
    except (ValueError, AttributeError):
        value = None
    if not isinstance(value, str) or not value.strip():
        raise UploadError(f"{verb} failed: {_OIDC} returned no token")
    return value


def _send(
    request: urllib.request.Request,
    timeout: float,
    where: str,
    *,
    read: int,
    verb: str = "upload",
    missing_ok: bool = False,
) -> bytes | None:
    """Make one request; anything but a 2xx answer is an ``UploadError`` naming ``where``.
    With ``missing_ok`` a 404 is ``None`` (nothing there yet), not an error."""
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return bytes(response.read(read))
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            exc.close()
            return None
        status = " ".join(filter(None, [f"HTTP {exc.code}", _printable(exc.reason)]))
        exc.close()
        raise UploadError(f"{verb} failed: {status} from {where}") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        if _timed_out(exc):
            raise UploadError(f"{verb} failed: no answer from {where} in {timeout:g}s") from None
        raise UploadError(f"{verb} failed: could not reach {where} ({_why(exc)})") from None


def _timed_out(exc: BaseException) -> bool:
    return isinstance(exc, socket.timeout) or isinstance(
        getattr(exc, "reason", None), socket.timeout
    )


def _why(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, str):
        return _printable(reason)
    return _printable(getattr(reason, "strerror", None) or type(reason).__name__)


def _printable(text: object, limit: int = 80) -> str:
    """One line of plain ASCII: a server or a file does not get to write to the terminal."""
    return re.sub(r"[^\x20-\x7e]", "?", str(text or ""))[:limit]
