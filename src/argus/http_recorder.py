"""HTTP recording and playback for deterministic replay.

Provides a context manager that intercepts outbound HTTP calls made by
node functions.  In **record** mode, request/response pairs are captured
alongside the run data.  In **playback** mode, recorded responses are
served back so that external API calls produce the same result as the
original run — making replay truly deterministic.

Works by monkey-patching two connection layers:

- ``urllib3.HTTPConnectionPool.urlopen`` (underpins ``requests``)
- ``httpcore``'s ``HTTPConnection.handle_request`` /
  ``AsyncHTTPConnection.handle_async_request`` (underpins ``httpx`` ≥0.28,
  which uses httpcore rather than urllib3 — F-30)

A request traverses exactly one of the two stacks, so nothing is recorded
twice. If a record session captures zero interactions while a backend was
patched, an ``argus`` logger warning says so — an empty ``.http.json`` is
no longer silent.

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
import logging
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("argus")

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


# ── Monkey-patching httpcore (httpx's connection layer, F-30) ────────────────
#
# httpx ≥0.28 speaks httpcore, not urllib3 — without this backend any
# httpx-based SDK traffic was invisible to record/playback. The patch points
# are httpcore's private connection modules; a version that moves them fails
# the guarded import and the backend is simply absent (the zero-capture
# warning below stays loud about it).

_original_handle_request = None
_original_handle_async_request = None


def _httpcore_full_url(request: Any) -> str:
    """Same shape the urllib3 backend builds: scheme://host:port/path?query."""
    u = request.url
    scheme = u.scheme.decode() if isinstance(u.scheme, bytes) else str(u.scheme)
    host = u.host.decode() if isinstance(u.host, bytes) else str(u.host)
    target = u.target.decode() if isinstance(u.target, bytes) else str(u.target)
    return f"{scheme}://{host}:{u.port}{target}"


def _tee_sync_stream(stream: Any) -> bytes:
    """Read a sync httpcore stream to bytes — the caller re-arms it."""
    if stream is None:
        return b""
    if hasattr(stream, "read"):
        data = stream.read()
        return data.encode() if isinstance(data, str) else data
    return b"".join(c.encode() if isinstance(c, str) else c for c in stream)


async def _tee_async_stream(stream: Any) -> bytes:
    if stream is None:
        return b""
    if hasattr(stream, "__aiter__"):
        chunks = []
        async for chunk in stream:
            chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
        return b"".join(chunks)
    return _tee_sync_stream(stream)  # a sync stream is fine to read here


class _AsyncListStream:
    """Re-arm a consumed stream for httpcore's async send path."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


def _headers_to_dict(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers or []:
        kk = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
        vv = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
        out[kk] = vv
    return out


def _playback_response(recorded: dict[str, Any]) -> Any:
    import httpcore  # type: ignore[import]

    return httpcore.Response(
        status=recorded["status"],
        headers=[
            (k.encode("utf-8"), v.encode("utf-8"))
            for k, v in (recorded.get("response_headers") or {}).items()
        ],
        content=(recorded.get("response_body") or "").encode("utf-8"),
    )


def _make_patched_handle_request(recorder: HttpRecorder | None, player: HttpPlayer | None):
    """Create a patched sync handle_request that records or plays back httpx traffic."""

    def _patched_handle_request(self, request):
        method = (
            request.method.decode()
            if isinstance(request.method, bytes)
            else str(request.method)
        )
        full_url = _httpcore_full_url(request)
        req_body = _tee_sync_stream(request.stream)
        request.stream = [req_body]  # re-arm for the real send

        if player is not None:
            recorded = player.lookup(method, full_url, req_body)
            if recorded is not None:
                return _playback_response(recorded)

        t0 = time.perf_counter()
        response = _original_handle_request(self, request)
        duration = (time.perf_counter() - t0) * 1000

        if recorder is not None:
            try:
                resp_body = _tee_sync_stream(response.stream)
                response.stream = [resp_body]
                recorder.record(
                    method=method,
                    url=full_url,
                    request_body=req_body,
                    status=getattr(response, "status", 0),
                    response_body=resp_body,
                    response_headers=_headers_to_dict(getattr(response, "headers", [])),
                    duration_ms=duration,
                )
            except Exception:
                pass  # recording is best-effort

        return response

    return _patched_handle_request


def _make_patched_handle_async_request(recorder: HttpRecorder | None, player: HttpPlayer | None):
    """Create a patched async handle_async_request that records or plays back httpx traffic."""

    async def _patched_handle_async_request(self, request):
        method = (
            request.method.decode()
            if isinstance(request.method, bytes)
            else str(request.method)
        )
        full_url = _httpcore_full_url(request)
        req_body = await _tee_async_stream(request.stream)
        request.stream = _AsyncListStream([req_body])

        if player is not None:
            recorded = player.lookup(method, full_url, req_body)
            if recorded is not None:
                return _playback_response(recorded)

        t0 = time.perf_counter()
        response = await _original_handle_async_request(self, request)
        duration = (time.perf_counter() - t0) * 1000

        if recorder is not None:
            try:
                resp_body = await _tee_async_stream(response.stream)
                response.stream = _AsyncListStream([resp_body])
                recorder.record(
                    method=method,
                    url=full_url,
                    request_body=req_body,
                    status=getattr(response, "status", 0),
                    response_body=resp_body,
                    response_headers=_headers_to_dict(getattr(response, "headers", [])),
                    duration_ms=duration,
                )
            except Exception:
                pass  # recording is best-effort

        return response

    return _patched_handle_async_request


def _patch_httpcore(recorder: HttpRecorder | None, player: HttpPlayer | None) -> bool:
    """Patch httpcore's sync+async connection entry points. Returns True when
    at least one backend was patched (False = httpcore absent or moved)."""
    global _original_handle_request, _original_handle_async_request

    patched = False
    try:
        from httpcore._async.connection import AsyncHTTPConnection
        from httpcore._sync.connection import HTTPConnection

        _original_handle_request = HTTPConnection.handle_request
        HTTPConnection.handle_request = _make_patched_handle_request(recorder, player)
        _original_handle_async_request = AsyncHTTPConnection.handle_async_request
        AsyncHTTPConnection.handle_async_request = _make_patched_handle_async_request(
            recorder, player
        )
        patched = True
    except Exception:
        pass  # httpcore absent or its internals moved — backend skipped
    return patched


def _unpatch_httpcore() -> None:
    global _original_handle_request, _original_handle_async_request

    if _original_handle_request is not None:
        from httpcore._sync.connection import HTTPConnection

        HTTPConnection.handle_request = _original_handle_request
        _original_handle_request = None
    if _original_handle_async_request is not None:
        from httpcore._async.connection import AsyncHTTPConnection

        AsyncHTTPConnection.handle_async_request = _original_handle_async_request
        _original_handle_async_request = None


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
    backends = 0

    try:
        import urllib3  # type: ignore[import]

        pool_cls = urllib3.HTTPConnectionPool
        _original_urlopen = pool_cls.urlopen
        pool_cls.urlopen = _make_patched_urlopen(recorder, None)
        backends += 1
    except ImportError:
        pool_cls = None  # urllib3 not installed — backend absent

    if _patch_httpcore(recorder, None):
        backends += 1

    if backends == 0:
        # No HTTP library present — recording is a no-op
        yield recorder
        return

    recorder.start()
    try:
        yield recorder
    finally:
        recorder.stop()
        if pool_cls is not None:
            pool_cls.urlopen = _original_urlopen
            _original_urlopen = None
        _unpatch_httpcore()
        if not recorder.interactions:
            logger.warning(
                "argus record_http captured zero HTTP interactions — if this "
                "workload uses httpx/requests, the traffic is not being seen "
                "(check that record_http wraps the actual call site)"
            )


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
        pool_cls = None

    _patch_httpcore(None, player)

    try:
        yield player
    finally:
        if pool_cls is not None:
            pool_cls.urlopen = _original_urlopen
            _original_urlopen = None
        _unpatch_httpcore()


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
