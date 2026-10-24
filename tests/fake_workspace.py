"""A local stand-in for GitHub's OIDC endpoint and a team workspace, on 127.0.0.1.

Standard library only (``http.server``), so the upload client is tested end to end over a real
socket with nothing leaving the machine. Used by tests/test_cloud_upload.py and
tests/test_data_leaks.py.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REQUEST_TOKEN = "runner-request-token-SECRET"  # ACTIONS_ID_TOKEN_REQUEST_TOKEN
OIDC_TOKEN = "oidc.jwt.SECRET"  # what the endpoint hands out


class FakeWorkspace:
    """Knobs a test turns, and the requests the client made."""

    def __init__(self) -> None:
        self.url = ""
        self.oidc_status = 200
        self.oidc_body: bytes | None = None  # replaces the {"value": token} answer
        self.upload_status = 200
        self.upload_body = b'{"ok": true}'
        self.upload_delay = 0.0
        self.redirect_to: str | None = None
        self.baselines_status = 404  # GET /api/v1/baselines: nothing learned yet
        self.baselines_body = b"{}"
        self.requests: list[dict[str, Any]] = []

    @property
    def oidc_url(self) -> str:
        return f"{self.url}/oidc?api-version=2.0"

    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]

    def uploaded(self) -> dict[str, Any]:
        """The JSON body of the one upload."""
        (post,) = self.posts()
        return json.loads(post["body"])


def _handler(fake: FakeWorkspace) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep the test output clean
            pass

        def _record(self, body: bytes = b"") -> None:
            fake.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )

        def _send(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except OSError:  # the client gave up (a timeout test)
                pass

        def do_GET(self) -> None:  # noqa: N802
            self._record()
            if self.path == "/api/v1/baselines":
                self._send(fake.baselines_status, fake.baselines_body)
            elif not self.path.startswith("/oidc"):
                self._send(404, b"{}")
            elif fake.oidc_body is not None:
                self._send(fake.oidc_status, fake.oidc_body)
            else:
                self._send(fake.oidc_status, json.dumps({"value": OIDC_TOKEN}).encode())

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._record(body)
            if fake.upload_delay:
                time.sleep(fake.upload_delay)
            if fake.redirect_to:
                self._send(307, b"{}", {"Location": fake.redirect_to})
            else:
                self._send(fake.upload_status, fake.upload_body)

    return Handler


@contextmanager
def serve(fake: FakeWorkspace) -> Iterator[FakeWorkspace]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def point_at(monkeypatch: Any, fake: FakeWorkspace) -> None:
    """Environment of a GitHub Actions job whose token endpoint and workspace are ``fake``."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", fake.oidc_url)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", REQUEST_TOKEN)
    monkeypatch.setenv("ARGUS_CLOUD_URL", fake.url)
    monkeypatch.setenv("no_proxy", "*")  # never route the loopback through a proxy
    monkeypatch.setenv("NO_PROXY", "*")
    for name in ("GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_REF"):  # this repo's own CI
        monkeypatch.delenv(name, raising=False)
