"""UI serving-path notices for VAR-112 (which ``.argus`` is live on 7842)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_UI_PORT = 7842
_UI_URL = f"http://localhost:{_UI_PORT}"


def count_run_json_files(runs_dir: Path) -> int:
    if not runs_dir.is_dir():
        return 0
    return len(list(runs_dir.glob("*.json")))


def format_ui_startup_lines(runs_dir: Path, run_count: int) -> list[str]:
    """Human-readable lines: which ``.argus`` is served, plus a zero-run warning."""
    lines = [
        f"Argus UI running at {_UI_URL}",
        f"serving runs from {runs_dir}",
    ]
    if run_count == 0:
        lines.append(
            "warning: 0 runs found. Invoke a graph with watcher.attach() first, "
            "or check $ARGUS_DIR / project root if you ran from another directory."
        )
    return lines


def format_port_busy_lines(
    local_runs_dir: Path,
    remote: dict[str, Any] | None,
) -> list[str]:
    """Warn when port 7842 is already bound, especially for a different project."""
    lines = [f"Argus UI already running at {_UI_URL}"]
    remote_runs = (remote or {}).get("runs_dir")
    if remote_runs:
        try:
            same = Path(str(remote_runs)).resolve() == local_runs_dir.resolve()
        except OSError:
            same = False
        if same:
            lines.append(f"serving runs from {local_runs_dir}")
        else:
            lines.append(
                "warning: that process is serving a different project "
                f"({remote_runs}), not {local_runs_dir}."
            )
    else:
        lines.append(
            f"warning: could not confirm which project owns port {_UI_PORT}. "
            f"This process would serve {local_runs_dir}."
        )
    return lines


def probe_running_ui(url: str = _UI_URL) -> dict[str, Any] | None:
    """Return ``/api/serving`` JSON from a UI already bound to 7842, if any."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/serving",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            data = json.loads(resp.read().decode())
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError, ValueError):
        return None
    if isinstance(data, dict) and "runs_dir" in data:
        return data
    return None
