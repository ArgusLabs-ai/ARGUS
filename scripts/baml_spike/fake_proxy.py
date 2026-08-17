"""Faithful local stand-in for ARGUS's two LLM transports.

The point of the B1 spike is to find out what a BAML-generated client actually
puts on the wire and what it does with what comes back. That question does not
need a real Supabase project or a real OpenAI key — it needs a server that
reproduces the *contract* exactly. This is that server.

``ProxyServer`` replicates supabase/functions/llm-proxy/index.ts:

    OPTIONS               -> 204 + CORS headers
    non-POST              -> 405 {"error": "POST only"}
    no bearer token       -> 401 {"error": "Missing authorization header"}
    unknown bearer token  -> 401 {"error": "Invalid or expired token. Run: argus login"}
    rate limited          -> 429 {"error": "Daily limit reached (200 calls/day)..."}
    unparseable body      -> 400 {"error": "Invalid JSON body"}
    upstream failure      -> upstream status, {"error": "<message>"}
    success               -> 200, the raw OpenAI response body, unmodified

Not replicated: index.ts patches defaults into the payload
(``model ?? "gpt-4o"``, ``max_tokens ?? 2000``, ``temperature ?? 0.3``) before
forwarding upstream. See ``_serve_proxy`` for why the spike wants them left
omitted.

Two properties matter and are reproduced deliberately:

  1. The function never inspects the request path. Supabase routes
     /functions/v1/<name>/<anything> to <name>, so a BAML client appending
     "/chat/completions" to the base URL still lands here. The server records
     every path it sees so the spike can report what BAML actually requested.

  2. Errors are a bare {"error": "<string>"}, NOT OpenAI's
     {"error": {"message": ..., "type": ...}} envelope. This is the single
     biggest shape mismatch a BAML client has to survive.

``UpstreamServer`` plays the OpenAI-compatible vendor API for the BYOK path,
and is also what the proxy forwards to.

Both are stdlib-only and bind to 127.0.0.1 on an ephemeral port. Nothing here
is imported by the argus package; it exists only for the spike.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

VALID_TOKEN = "spike-valid-supabase-jwt"  # noqa: S105 - fixture, not a credential
DAILY_LIMIT = 200


class _Recorder:
    """Shared, thread-safe log of what the client put on the wire."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(entry)

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()

    @property
    def last(self) -> dict[str, Any] | None:
        with self._lock:
            return self.requests[-1] if self.requests else None

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.requests)


def _completion_body(content: str) -> dict[str, Any]:
    """A realistic OpenAI chat-completions success body."""
    return {
        "id": "chatcmpl-spike",
        "object": "chat.completion",
        "created": 1_755_400_000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 612, "completion_tokens": 148, "total_tokens": 760},
    }


DEFAULT_CONTENT = json.dumps(
    {
        "pass": True,
        "reason": "Output is a plausible transformation of the input.",
        "confidence": 0.82,
        "evidence_considered": ["validator:required_fields"],
        "overridden_signals": [],
        "disambiguation_verdicts": [],
    }
)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ArgusSpike/1.0"

    # Silence per-request logging; the harness prints its own report.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        pass

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> tuple[dict[str, Any] | None, bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw), raw
        except Exception:
            return None, raw

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type, apikey")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._send(405, {"error": "POST only"})

    def do_POST(self) -> None:  # noqa: N802
        cfg = self.server.spike_config  # type: ignore[attr-defined]
        rec = self.server.recorder  # type: ignore[attr-defined]

        body, raw = self._read_body()
        rec.record(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
                "raw_len": len(raw),
            }
        )

        if cfg["mode"] == "hang":
            # Never respond. Used to observe client-side timeout behaviour.
            threading.Event().wait(cfg["hang_seconds"])
            return

        if cfg["role"] == "proxy":
            self._serve_proxy(cfg, body)
        else:
            self._serve_upstream(cfg, body)

    # -- the Supabase edge function contract ------------------------------
    def _serve_proxy(self, cfg: dict[str, Any], body: dict[str, Any] | None) -> None:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:] if auth[:7].lower() == "bearer " else ""

        if not token:
            self._send(401, {"error": "Missing authorization header"})
            return
        if token != cfg["valid_token"]:
            self._send(401, {"error": "Invalid or expired token. Run: argus login"})
            return
        if cfg["mode"] == "rate_limited":
            self._send(
                429,
                {
                    "error": (
                        f"Daily limit reached ({DAILY_LIMIT} calls/day). "
                        "Set your own OPENAI_API_KEY to remove limits."
                    )
                },
            )
            return
        if body is None:
            self._send(400, {"error": "Invalid JSON body"})
            return
        if cfg["mode"] == "upstream_error":
            # index.ts flattens OpenAI's error object down to a bare string.
            self._send(400, {"error": "This model does not support response_format."})
            return
        if cfg["mode"] == "proxy_exception":
            self._send(502, {"error": "Proxy error: connection reset"})
            return

        # Simplification: index.ts fills in `model ?? "gpt-4o"`,
        # `max_tokens ?? 2000` and `temperature ?? 0.3` before forwarding
        # upstream. This replica does not, because every probe measures what
        # BAML puts on the wire, not what OpenAI would receive after the edge
        # function has patched it — P3 in particular is only meaningful if an
        # omitted field stays omitted here.
        self._send(200, _completion_body(cfg["content"]))

    # -- an OpenAI-compatible vendor API ----------------------------------
    def _serve_upstream(self, cfg: dict[str, Any], body: dict[str, Any] | None) -> None:
        if body is None:
            self._send(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request"}})
            return
        if cfg["mode"] == "upstream_error":
            self._send(
                401,
                {"error": {"message": "Incorrect API key provided.", "type": "invalid_request"}},
            )
            return
        self._send(200, _completion_body(cfg["content"]))


class SpikeServer:
    """A running fake, addressable as ``server.base_url``."""

    def __init__(self, role: str = "proxy") -> None:
        self.recorder = _Recorder()
        self.config: dict[str, Any] = {
            "role": role,
            "mode": "ok",
            "content": DEFAULT_CONTENT,
            "valid_token": VALID_TOKEN,
            "hang_seconds": 30.0,
        }
        # Threading, not HTTPServer: the "hang" mode holds a connection open,
        # and a serialized server would make every later probe's timing a
        # measurement of the previous probe.
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.spike_config = self.config  # type: ignore[attr-defined]
        self._httpd.recorder = self.recorder  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> SpikeServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        """The function root, mirroring {SUPABASE_URL}/functions/v1/llm-proxy."""
        return f"http://127.0.0.1:{self.port}/functions/v1/llm-proxy"

    def set(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

    def reset(self) -> None:
        self.recorder.reset()
        self.config["mode"] = "ok"
        self.config["content"] = DEFAULT_CONTENT
