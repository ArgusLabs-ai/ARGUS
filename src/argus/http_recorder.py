"""HTTP recording and playback for deterministic replay.

Provides a context manager that intercepts outbound HTTP calls made by
node functions.  In **record** mode, request/response pairs are captured
alongside the run data.  In **playback** mode, recorded responses are
served back so that external API calls produce the same result as the
original run — making replay truly deterministic.

Works by monkey-patching ``urllib3.HTTPConnectionPool.urlopen`` (which
underpins ``requests``, ``httpx``, and most Python HTTP libraries).

Usage — recording::

    watcher = ArgusWatcher(record_http=True)
    watcher.watch(graph)
    app = graph.compile()
    app.invoke(state)

Usage — automatic playback during replay::

    argus replay <run-id> <node>   # uses recorded HTTP if available

The recorded interactions are stored in ``.argus/runs/<run-id>.http.json``
alongside the normal run JSON.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# ── Interaction model ────────────────────────────────────────────────────────


def _request_key(method: str, url: str, body: bytes | str | None) -> str:
    """Produce a stable hash key for a request (method + url + body hash)."""
    body_bytes = b""
    if body is not None:
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    body_hash = hashlib.sha256(body_bytes).hexdigest()[:16]
    return f"{method}|{url}|{body_hash}"


# ── Recording ────────────────────────────────────────────────────────────────


class HttpRecorder:
    """Thread-safe recorder that captures HTTP request/response pairs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interactions: list[dict[str, Any]] = []
        self._active = False

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    @property
    def interactions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._interactions)

    def record(
        self,
        method: str,
        url: str,
        request_body: bytes | str | None,
        status: int,
        response_body: bytes,
        response_headers: dict[str, str],
        duration_ms: float,
    ) -> None:
        if not self._active:
            return
        entry = {
            "method": method,
            "url": url,
            "request_body_hash": hashlib.sha256(
                (request_body or b"")
                if isinstance(request_body, bytes)
                else (request_body or "").encode()
            ).hexdigest()[:16],
            "status": status,
            "response_body": response_body.decode("utf-8", errors="replace"),
            "response_headers": response_headers,
            "duration_ms": round(duration_ms, 2),
            "key": _request_key(method, url, request_body),
        }
        with self._lock:
            self._interactions.append(entry)


# ── Playback ─────────────────────────────────────────────────────────────────


class HttpPlayer:
    """Serves pre-recorded HTTP responses during replay."""

    def __init__(self, interactions: list[dict[str, Any]]) -> None:
        # Build a lookup: key → list of responses (FIFO for repeated calls)
        self._responses: dict[str, list[dict[str, Any]]] = {}
        for entry in interactions:
            key = entry["key"]
            self._responses.setdefault(key, []).append(entry)
        self._lock = threading.Lock()
        self._miss_count = 0

    def lookup(
        self,
        method: str,
        url: str,
        body: bytes | str | None,
    ) -> dict[str, Any] | None:
        """Find a recorded response for this request. Returns None on miss."""
        key = _request_key(method, url, body)
        with self._lock:
            entries = self._responses.get(key)
            if entries:
                return entries.pop(0)
            self._miss_count += 1
            return None

    @property
    def miss_count(self) -> int:
        return self._miss_count


# ── Monkey-patching urllib3 ──────────────────────────────────────────────────


_original_urlopen = None


def _make_patched_urlopen(recorder: HttpRecorder | None, player: HttpPlayer | None):
    """Create a patched urlopen that records or plays back HTTP calls."""

    def _patched_urlopen(self, method, url, body=None, headers=None, **kwargs):
        full_url = f"{self.scheme}://{self.host}:{self.port}{url}"

        # Playback mode: serve recorded response if available
        if player is not None:
            recorded = player.lookup(method, full_url, body)
            if recorded is not None:
                # Build a fake urllib3 response
                from unittest.mock import MagicMock

                resp = MagicMock()
                resp.status = recorded["status"]
                resp.data = recorded["response_body"].encode("utf-8")
                resp.headers = recorded.get("response_headers", {})
                resp.read.return_value = resp.data
                resp.getheader = lambda h, d=None: resp.headers.get(h, d)
                resp.getheaders.return_value = resp.headers
                return resp

        # Record mode or passthrough: make the real call
        t0 = time.perf_counter()
        response = _original_urlopen(self, method, url, body=body, headers=headers, **kwargs)
        duration = (time.perf_counter() - t0) * 1000

        if recorder is not None:
            try:
                resp_body = response.data if hasattr(response, "data") else b""
                resp_headers = dict(response.headers) if hasattr(response, "headers") else {}
                recorder.record(
                    method=method,
                    url=full_url,
                    request_body=body,
                    status=getattr(response, "status", 0),
                    response_body=resp_body,
                    response_headers=resp_headers,
                    duration_ms=duration,
                )
            except Exception:
                pass  # recording is best-effort

        return response

    return _patched_urlopen


@contextmanager
def record_http() -> Generator[HttpRecorder, None, None]:
    """Context manager that records all outbound HTTP calls.

    Usage::

        with record_http() as recorder:
            app.invoke(state)
        # recorder.interactions contains all captured HTTP calls
    """
    global _original_urlopen

    recorder = HttpRecorder()

    try:
        import urllib3  # type: ignore[import]

        pool_cls = urllib3.HTTPConnectionPool
        _original_urlopen = pool_cls.urlopen
        pool_cls.urlopen = _make_patched_urlopen(recorder, None)
        recorder.start()
    except ImportError:
        # urllib3 not installed — recording is a no-op
        yield recorder
        return

    try:
        yield recorder
    finally:
        recorder.stop()
        pool_cls.urlopen = _original_urlopen
        _original_urlopen = None


@contextmanager
def playback_http(interactions: list[dict[str, Any]]) -> Generator[HttpPlayer, None, None]:
    """Context manager that serves pre-recorded HTTP responses.

    Usage::

        with playback_http(recorded_interactions) as player:
            app.invoke(state)
        # player.miss_count shows how many calls had no recording
    """
    global _original_urlopen

    player = HttpPlayer(interactions)

    try:
        import urllib3  # type: ignore[import]

        pool_cls = urllib3.HTTPConnectionPool
        _original_urlopen = pool_cls.urlopen
        pool_cls.urlopen = _make_patched_urlopen(None, player)
    except ImportError:
        yield player
        return

    try:
        yield player
    finally:
        pool_cls.urlopen = _original_urlopen
        _original_urlopen = None


# ── Storage ──────────────────────────────────────────────────────────────────


def save_http_interactions(run_id: str, interactions: list[dict[str, Any]]) -> Path:
    """Save recorded HTTP interactions alongside the run JSON."""
    from argus.storage import _runs_path

    path = _runs_path() / f"{run_id}.http.json"
    path.write_text(json.dumps(interactions, indent=2), encoding="utf-8")
    return path


def load_http_interactions(run_id: str) -> list[dict[str, Any]] | None:
    """Load recorded HTTP interactions for a run. Returns None if not found."""
    from argus.storage import _candidate_runs_dirs  # noqa: PLC0415

    for directory in _candidate_runs_dirs():
        candidate = directory / f"{run_id}.http.json"
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None
