"""B-10 (F-30): record_http must see httpx traffic (httpcore backend).

httpx ≥0.28 speaks httpcore, not urllib3 — the urllib3-only patch left
httpx workloads with a silently empty .http.json. These tests drive the
patched handle_request/handle_async_request directly with a stubbed
original (zero network) and pin the zero-capture log warning.
"""

from __future__ import annotations

import asyncio
import logging

import httpcore
import pytest
from httpcore._async.connection import AsyncHTTPConnection
from httpcore._sync.connection import HTTPConnection

import argus.http_recorder as hr


@pytest.fixture
def restore_handle_request():
    """_unpatch_httpcore restores whatever the module global holds — a test
    stub included — so put the true originals back afterwards."""
    saved_sync = HTTPConnection.handle_request
    saved_async = AsyncHTTPConnection.handle_async_request
    yield
    HTTPConnection.handle_request = saved_sync
    AsyncHTTPConnection.handle_async_request = saved_async


class _AsyncBytes:
    def __init__(self, data: bytes):
        self._chunks = [data]

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


def test_httpcore_backend_records_sync(restore_handle_request):
    sent = []

    def fake_handle(self, request):
        sent.append(request)
        return httpcore.Response(
            201,
            headers=[(b"content-type", b"application/json"), (b"x-test", b"yes")],
            content=b'{"ok": true}',
        )

    with hr.record_http() as recorder:
        hr._original_handle_request = fake_handle  # wrapper reads the global at call time
        req = httpcore.Request(
            b"POST",
            b"https://example.com:443/v1/search?q=x",
            headers=[(b"h", b"v")],
            content=b'{"query": "argus"}',
        )
        resp = HTTPConnection.handle_request(object(), req)

    assert resp.status == 201
    assert b"".join(resp.stream) == b'{"ok": true}'  # response stream re-armed
    assert sent and b"".join(sent[0].stream) == b'{"query": "argus"}'  # request re-armed
    assert len(recorder.interactions) == 1
    entry = recorder.interactions[0]
    assert entry["method"] == "POST"
    assert entry["url"] == "https://example.com:443/v1/search?q=x"
    assert entry["status"] == 201
    assert entry["response_body"] == '{"ok": true}'
    assert entry["response_headers"]["x-test"] == "yes"


def test_httpcore_backend_playback_sync(restore_handle_request):
    url = "https://example.com:443/v1/x"
    recorded = [
        {
            "method": "GET",
            "url": url,
            "request_body_hash": "0" * 16,
            "status": 200,
            "response_body": "cached",
            "response_headers": {"x-cached": "1"},
            "duration_ms": 1.0,
            "key": hr._request_key("GET", url, b""),
        }
    ]

    def boom(self, request):
        raise AssertionError("real send must not run on a playback hit")

    with hr.playback_http(recorded) as player:
        hr._original_handle_request = boom
        resp = HTTPConnection.handle_request(
            object(), httpcore.Request(b"GET", url.encode())
        )
        assert resp.status == 200
        assert b"".join(resp.stream) == b"cached"

    assert player.miss_count == 0


def test_zero_capture_logs_warning(restore_handle_request, caplog):
    with caplog.at_level(logging.WARNING, logger="argus"):
        with hr.record_http() as recorder:
            pass  # no HTTP traffic at all
    assert recorder.interactions == []
    assert any("zero HTTP interactions" in r.getMessage() for r in caplog.records)


def test_httpcore_backend_records_async(restore_handle_request):
    async def fake_handle(self, request):
        return httpcore.Response(200, headers=[], content=_AsyncBytes(b"async-ok"))

    async def main():
        with hr.record_http() as recorder:
            hr._original_handle_async_request = fake_handle
            req = httpcore.Request(b"GET", b"https://example.com:443/a")
            resp = await AsyncHTTPConnection.handle_async_request(object(), req)
            assert resp.status == 200
            body = b""
            async for chunk in resp.stream:
                body += chunk
            assert body == b"async-ok"  # async response stream re-armed
        return recorder

    recorder = asyncio.run(main())
    assert len(recorder.interactions) == 1
    assert recorder.interactions[0]["url"] == "https://example.com:443/a"
    assert recorder.interactions[0]["response_body"] == "async-ok"


def test_urllib3_backend_still_patched(restore_handle_request):
    """Both backends patched in record mode — the urllib3 path is unchanged."""
    import urllib3

    with hr.record_http():
        assert urllib3.HTTPConnectionPool.urlopen is not None
        assert (
            urllib3.HTTPConnectionPool.urlopen.__name__ == "_patched_urlopen"
        )
    assert urllib3.HTTPConnectionPool.urlopen.__name__ != "_patched_urlopen"
