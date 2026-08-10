"""OAuth login flow for ARGUS cloud sync.

Opens the browser for Google auth via Supabase, captures the
callback on a temporary local server.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

from rich.console import Console

from argus.cloud import (
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    Credentials,
    clear_credentials,
    is_logged_in,
    load_credentials,
    save_credentials,
)

_console = Console()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


_CALLBACK_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>ARGUS — Login</title>
  <style>
    body {
      font-family: -apple-system, system-ui, sans-serif;
      background: #0a0a0a; color: #e5e5e5;
      display: flex; align-items: center; justify-content: center;
      height: 100vh; margin: 0;
    }
    .card {
      text-align: center; padding: 3rem;
      border: 1px solid #222; border-radius: 12px;
      background: #111;
    }
    h1 { font-size: 1.2rem; margin-bottom: 0.5rem; }
    p  { color: #888; font-size: 0.85rem; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Logged in to ARGUS</h1>
    <p>You can close this tab and return to your terminal.</p>
  </div>
  <script>
    // Supabase puts tokens in the URL hash fragment
    const hash = window.location.hash.substring(1);
    if (hash) {
      fetch('/callback_token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: hash,
      });
    }
  </script>
</body>
</html>
"""


def login() -> None:
    """Run the OAuth login flow."""
    if SUPABASE_URL is None:
        _console.print(
            "  [yellow]Cloud login is a hosted-only feature.[/yellow]\n"
            "  ARGUS runs fully local with your own key — set one with: "
            "[bold]argus key set[/bold]\n"
            "  or export OPENAI_API_KEY."
        )
        return
    if is_logged_in():
        creds = load_credentials()
        if creds and time.time() < creds.expires_at - 60:
            _console.print(
                f"[green]Already logged in[/green] as [bold]{creds.email}[/bold]\n"
                "  Run [bold]argus logout[/bold] first to switch accounts."
            )
            return
        # Token expired — clear it and proceed with fresh login
        clear_credentials()

    port = _find_free_port()
    redirect_url = f"http://localhost:{port}/callback"

    # Build the Supabase OAuth URL
    params = urlencode(
        {
            "provider": "google",
            "redirect_to": redirect_url,
        }
    )
    auth_url = f"{SUPABASE_URL}/auth/v1/authorize?{params}"

    result: dict[str, str] = {}
    server_ready = threading.Event()
    got_token = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:
            # Serve the callback page that reads the hash
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_CALLBACK_HTML.encode())

        def do_POST(self) -> None:
            if self.path == "/callback_token":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                # Parse the hash fragment: access_token=...&refresh_token=...
                for pair in body.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        result[k] = v
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                got_token.set()
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(("localhost", port), Handler)

    def serve() -> None:
        server_ready.set()
        server.serve_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    server_ready.wait()

    _console.print()
    _console.print("[bold]Opening browser for Google sign-in...[/bold]")
    _console.print("  [dim]If the browser doesn't open, visit:[/dim]")
    _console.print(f"  [dim]{auth_url}[/dim]")
    _console.print()

    webbrowser.open(auth_url)

    # Wait for the callback (up to 120 seconds)
    if not got_token.wait(timeout=120):
        server.shutdown()
        _console.print("[red]Login timed out.[/red] Try again with [bold]argus login[/bold].")
        raise SystemExit(1)

    server.shutdown()

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in", "3600"))

    if not access_token:
        _console.print("[red]Login failed — no access token received.[/red]")
        raise SystemExit(1)

    # Fetch user info from Supabase
    import urllib.request

    req = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {access_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            user_data = json.loads(resp.read())
    except Exception as exc:
        _console.print(f"[red]Failed to fetch user info:[/red] {exc}")
        raise SystemExit(1)

    creds = Credentials(
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=user_data["id"],
        email=user_data.get("email", "unknown"),
        expires_at=time.time() + expires_in,
    )
    save_credentials(creds)

    _console.print(f"[green]Logged in[/green] as [bold]{creds.email}[/bold]")
    _console.print("  [dim]Credentials saved to ~/.argus/credentials.json[/dim]")
    _console.print("  [dim]Runs will now sync to the cloud automatically.[/dim]")


def logout() -> None:
    """Clear stored credentials."""
    if not is_logged_in():
        _console.print("[dim]Not logged in.[/dim]")
        return
    creds = load_credentials()
    clear_credentials()
    _console.print(f"[green]Logged out[/green] from [bold]{creds.email}[/bold]")


def whoami() -> None:
    """Show current login status."""
    creds = load_credentials()
    if creds is None:
        _console.print(
            "[dim]Not logged in.[/dim] Run [bold]argus login[/bold] to sync runs to the cloud."
        )
        return
    _console.print(f"Logged in as [bold]{creds.email}[/bold]")
    _console.print(f"  [dim]user_id:[/dim] {creds.user_id}")
