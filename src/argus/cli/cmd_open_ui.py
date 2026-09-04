from __future__ import annotations

import importlib
import json
import mimetypes
import os
import socket
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from rich.console import Console

from argus.cli.cmd_doctor import (
    _check_langgraph,
    _check_optional_deps,
    _check_python_version,
    _check_replay_readiness,
    _check_storage,
)
from argus.cli.ui_serving import (
    count_run_json_files,
    format_port_busy_lines,
    format_ui_startup_lines,
    probe_running_ui,
)
from argus.storage import argus_dir, resolve_project_root
from argus.storage import runs_dir as resolved_runs_dir

_console = Console()
_UI_PORT = 7842


class _SkipWebhook(Exception):
    """Sentinel to skip Discord webhook when not configured."""


_UI_URL = f"http://localhost:{_UI_PORT}"
_DIST_DIR = Path(__file__).parent.parent / "ui_dist"

# ── Replay job registry ────────────────────────────────────────────────────
_replay_jobs: dict[str, dict] = {}
_replay_lock = threading.Lock()


def _get_cli_auth() -> dict | None:
    """Return CLI credentials for auto-login in the local UI, refreshing if needed."""
    try:
        from argus.cloud import _get_valid_credentials  # noqa: PLC0415

        creds = _get_valid_credentials()
        if creds is None:
            return None
        return {
            "access_token": creds.access_token,
            "refresh_token": creds.refresh_token,
            "email": creds.email,
        }
    except Exception:
        return None


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", _UI_PORT)) == 0


def _content_type(suffix: str) -> str:
    ct, _ = mimetypes.guess_type(f"file{suffix}")
    return ct or "application/octet-stream"


_GLOBAL_CONFIG = Path.home() / ".argus" / "config.json"


def _get_discord_webhook() -> str:
    """Resolve Discord webhook: env var > project config > global ~/.argus/config.json."""
    env = os.environ.get("ARGUS_DISCORD_WEBHOOK", "")
    if env:
        return env
    local = _load_config().get("discord_webhook", "")
    if local:
        return local
    # Fall back to global user config
    try:
        return json.loads(_GLOBAL_CONFIG.read_text()).get("discord_webhook", "")
    except Exception:
        return ""


def _collect_doctor_info() -> dict:
    """Run all doctor checks and return structured results."""
    checks = [
        ("python", _check_python_version),
        ("langgraph", _check_langgraph),
        ("storage", _check_storage),
        ("replay", _check_replay_readiness),
        ("optional_deps", _check_optional_deps),
    ]
    results = {}
    for name, fn in checks:
        try:
            passed, message = fn()
        except Exception as e:
            passed, message = False, f"check failed: {e}"
        results[name] = {"passed": passed, "message": message}

    from argus import __version__

    results["argus_version"] = __version__
    return results


def _sanitize_run_for_report(run_data: dict) -> dict:
    """Extract all diagnostic fields from a run — no user data.

    Includes everything needed to debug remotely: inspection details,
    tool failures with evidence, anomaly signals, semantic checks,
    correlation analysis, LLM investigation, and validator results.
    Excludes: input_state, output_dict, initial_state (user data).
    """
    steps = []
    for s in run_data.get("steps", []):
        step_info: dict = {
            "step_index": s.get("step_index"),
            "node_name": s.get("node_name"),
            "status": s.get("status"),
            "duration_ms": s.get("duration_ms"),
            "timestamp_utc": s.get("timestamp_utc"),
            "behavior_type": s.get("behavior_type"),
            "attempt_index": s.get("attempt_index", 0),
            "is_subgraph_entry": s.get("is_subgraph_entry"),
            # Shape info without actual data
            "input_keys": list((s.get("input_state") or {}).keys()),
            "output_keys": list((s.get("output_dict") or {}).keys()),
        }
        insp = s.get("inspection")
        if insp:
            step_info["inspection"] = {
                "is_silent_failure": insp.get("is_silent_failure"),
                "has_tool_failure": insp.get("has_tool_failure"),
                "severity": insp.get("severity"),
                "message": insp.get("message"),
                "missing_fields": insp.get("missing_fields", []),
                "empty_fields": insp.get("empty_fields", []),
                "type_mismatches": insp.get("type_mismatches", []),
                "suspicious_empty_keys": insp.get("suspicious_empty_keys", []),
                "unannotated_successors": insp.get("unannotated_successors", []),
                "degraded_fields": insp.get("degraded_fields", []),
                "degraded_upstream_node": insp.get("degraded_upstream_node"),
                "tool_failures": [
                    {
                        "failure_type": tf.get("failure_type"),
                        "field_name": tf.get("field_name"),
                        "severity": tf.get("severity"),
                        "evidence": tf.get("evidence"),
                    }
                    for tf in (insp.get("tool_failures") or [])
                ],
                "semantic_signals": [
                    {
                        "sig_id": ss.get("sig_id"),
                        "category": ss.get("category"),
                        "severity": ss.get("severity"),
                        "description": ss.get("description"),
                        "field_path": ss.get("field_path"),
                        "evidence": ss.get("evidence"),
                    }
                    for ss in (insp.get("semantic_signals") or [])
                ],
            }
        exc = s.get("exception")
        if exc:
            step_info["exception"] = exc
        sc = s.get("semantic_check")
        if sc:
            step_info["semantic_check"] = {
                "passed": sc.get("passed"),
                "confidence": sc.get("confidence"),
                "reason": sc.get("reason"),
                "model": sc.get("model"),
                "duration_ms": sc.get("duration_ms"),
            }
        anomalies = s.get("anomaly_signals")
        if anomalies:
            step_info["anomaly_signals"] = [
                {
                    "anomaly_id": a.get("anomaly_id"),
                    "severity": a.get("severity"),
                    "suspicion_score": a.get("suspicion_score"),
                    "reason": a.get("reason"),
                    "expected_behavior": a.get("expected_behavior"),
                    "observed_behavior": a.get("observed_behavior"),
                    "field_path": a.get("field_path"),
                }
                for a in anomalies
            ]
        vr = s.get("validator_results")
        if vr:
            step_info["validator_results"] = [
                {
                    "validator_name": v.get("validator_name"),
                    "is_valid": v.get("is_valid"),
                    "message": v.get("message"),
                }
                for v in vr
            ]
        dr = s.get("disambiguation_results")
        if dr:
            step_info["disambiguation_results"] = dr
        lu = s.get("llm_usage")
        if lu:
            step_info["llm_usage"] = {
                "total_tokens": lu.get("total_tokens"),
                "total_cost_usd": lu.get("total_cost_usd"),
            }
        steps.append(step_info)

    report: dict = {
        "run_id": run_data.get("run_id"),
        "argus_version": run_data.get("argus_version"),
        "overall_status": run_data.get("overall_status"),
        "started_at": run_data.get("started_at"),
        "completed_at": run_data.get("completed_at"),
        "duration_ms": run_data.get("duration_ms"),
        "first_failure_step": run_data.get("first_failure_step"),
        "root_cause_chain": run_data.get("root_cause_chain", []),
        "graph_node_names": run_data.get("graph_node_names", []),
        "graph_edge_map": run_data.get("graph_edge_map", {}),
        "is_cyclic": run_data.get("is_cyclic", False),
        "interrupted": run_data.get("interrupted", False),
        "interrupt_node": run_data.get("interrupt_node"),
        "total_llm_calls": run_data.get("total_llm_calls", 0),
        "total_tokens": run_data.get("total_tokens", 0),
        "total_cost_usd": run_data.get("total_cost_usd"),
        "steps": steps,
    }

    # Correlation analysis
    corr = run_data.get("correlation")
    if corr:
        report["correlation"] = {
            "causal_summary": corr.get("causal_summary"),
            "degradation_origins": corr.get("degradation_origins", []),
            "propagation_chains": corr.get("propagation_chains", []),
            "timeline": corr.get("timeline", []),
        }
        li = corr.get("llm_insight")
        if li:
            report["correlation"]["llm_insight"] = li

    # LLM investigation
    inv = run_data.get("llm_investigation")
    if inv:
        report["llm_investigation"] = {
            "triggered": inv.get("triggered"),
            "trigger_reasons": inv.get("trigger_reasons", []),
            "root_cause_explanation": inv.get("root_cause_explanation"),
            "causal_hypotheses": inv.get("causal_hypotheses", []),
            "degradation_narrative": inv.get("degradation_narrative"),
            "observations": inv.get("observations", []),
            "debugging_suggestions": inv.get("debugging_suggestions", []),
            "confidence": inv.get("confidence"),
            "model_used": inv.get("model_used"),
            "investigation_duration_ms": inv.get("investigation_duration_ms"),
            "error": inv.get("error"),
        }

    return report


def _config_path() -> Path:
    return argus_dir() / "config.json"


_LINEAR_API_URL = "https://api.linear.app/graphql"

# Category → Linear label name mapping
_CATEGORY_LABEL_MAP = {
    "bug": "Bug",
    "feature": "Feature",
    "improvement": "Improvement",
    "setup_issue": "Setup Issue",
    "unexpected_result": "Unexpected Result",
}


def _load_config() -> dict:
    """Read the full .argus/config.json."""
    try:
        return json.loads(_config_path().read_text())
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    """Write the full .argus/config.json (merge with existing)."""
    _config_path().parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(_config_path().read_text())
    except Exception:
        existing = {}
    existing.update(data)
    _config_path().write_text(json.dumps(existing, indent=2))
    _config_path().chmod(0o600)


def _linear_graphql(api_key: str, query: str, variables: dict | None = None) -> dict:
    """Execute a Linear GraphQL query. Raises on failure."""
    import urllib.request  # noqa: PLC0415

    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _LINEAR_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _linear_get_teams(api_key: str) -> list[dict]:
    """Fetch teams from Linear."""
    result = _linear_graphql(api_key, "{ teams { nodes { id name key } } }")
    return result.get("data", {}).get("teams", {}).get("nodes", [])


def _linear_get_labels(api_key: str, team_id: str) -> list[dict]:
    """Fetch labels for a Linear team."""
    query = """
    query($teamId: String!) {
      issueLabels(filter: { team: { id: { eq: $teamId } } }) {
        nodes { id name color }
      }
    }
    """
    result = _linear_graphql(api_key, query, {"teamId": team_id})
    return result.get("data", {}).get("issueLabels", {}).get("nodes", [])


def _linear_find_or_create_label(api_key: str, team_id: str, label_name: str) -> str | None:
    """Find a label by name, or create it. Returns label ID or None."""
    labels = _linear_get_labels(api_key, team_id)
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return label["id"]
    # Create the label
    mutation = """
    mutation($input: IssueLabelCreateInput!) {
      issueLabelCreate(input: $input) {
        issueLabel { id name }
        success
      }
    }
    """
    result = _linear_graphql(
        api_key,
        mutation,
        {
            "input": {"name": label_name, "teamId": team_id},
        },
    )
    created = result.get("data", {}).get("issueLabelCreate", {})
    if created.get("success"):
        return created.get("issueLabel", {}).get("id")
    return None


def _send_to_linear(
    api_key: str,
    team_id: str,
    category: str,
    title: str,
    description_md: str,
) -> dict | None:
    """Create a Linear issue. Returns {"id", "identifier", "url"} or None."""
    label_name = _CATEGORY_LABEL_MAP.get(category, category.replace("_", " ").title())
    label_id = _linear_find_or_create_label(api_key, team_id, label_name)

    mutation = """
    mutation($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        issue { id identifier url title }
        success
      }
    }
    """
    issue_input: dict = {
        "title": title,
        "description": description_md,
        "teamId": team_id,
    }
    if label_id:
        issue_input["labelIds"] = [label_id]

    result = _linear_graphql(api_key, mutation, {"input": issue_input})
    created = result.get("data", {}).get("issueCreate", {})
    if created.get("success"):
        issue = created.get("issue", {})
        return {
            "id": issue.get("id"),
            "identifier": issue.get("identifier"),
            "url": issue.get("url"),
            "title": issue.get("title"),
        }
    return None


def _aliases_path(project_dir: Path) -> Path:
    return project_dir / ".argus" / "aliases.json"


def _load_aliases(project_dir: Path) -> dict[str, str]:
    """Read {run_id: alias} map from .argus/aliases.json."""
    try:
        return json.loads(_aliases_path(project_dir).read_text())
    except Exception:
        return {}


def _save_aliases(aliases: dict[str, str], project_dir: Path) -> None:
    p = _aliases_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(aliases, indent=2))


def _load_config_app_factory() -> str | None:
    """Read default app factory from .argus/config.json in CWD."""
    try:
        data = json.loads(_config_path().read_text())
        return data.get("app") or None
    except Exception:
        return None


def _save_config_app_factory(app: str) -> None:
    """Write app factory to .argus/config.json."""
    _config_path().parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(_config_path().read_text())
    except Exception:
        existing = {}
    existing["app"] = app
    _config_path().write_text(json.dumps(existing, indent=2))


def _import_factory_for_ui(spec: str):
    """Import the app factory callable from a 'module:fn' spec. Raises on failure."""
    if ":" not in spec:
        raise ValueError(f"app must be 'module:function', got: '{spec}'")
    module_path, fn_name = spec.rsplit(":", 1)
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(f"Cannot import '{module_path}': {e}") from e
    fn = getattr(module, fn_name, None)
    if fn is None or not callable(fn):
        raise ValueError(f"'{fn_name}' not found or not callable in '{module_path}'")
    return fn


_MAX_PREVIEW_VALUE_CHARS = 2000


class _PatchRequestError(Exception):
    """A patch request that must be rejected, carrying its wire response."""

    def __init__(self, error: str, message: str, status: int) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status

    def as_response(self) -> tuple[dict[str, str], int]:
        return {"error": self.error, "message": self.message}, self.status


def _resolve_patch_preview(
    run_record: Any,
    from_step: str,
    patch: Any,
    create_missing: bool,
) -> list[Any]:
    """Validate a patch against a run's recorded state and return its changes.

    Shared by ``/api/replay`` — which discards the result and only wants the
    validation — and ``/api/replay/preview``, which returns it. One code path
    means a preview can never report a patch as valid that the real replay
    would then reject.
    """
    from argus.state_patch import (  # noqa: PLC0415
        PatchError,
        preview_patch,
        validate_patch,
    )

    if not isinstance(patch, dict):
        raise _PatchRequestError(
            "invalid_patch",
            f"patch must be a JSON object, got {type(patch).__name__}",
            400,
        )

    target = next((s for s in run_record.steps if s.node_name == from_step), None)
    if target is None:
        available = [s.node_name for s in run_record.steps]
        raise _PatchRequestError(
            "unknown_node",
            f"node '{from_step}' not found in run. Available: {', '.join(available)}",
            404,
        )

    try:
        validate_patch(patch)
        # Dry-run against the real recorded state so a bad path is rejected
        # before anything is executed or enqueued.
        return preview_patch(target.input_state, patch, create_missing=create_missing)
    except PatchError as exc:
        raise _PatchRequestError("invalid_patch", str(exc), 400) from exc


def _cap_preview_value(value: Any) -> Any:
    """Cap one preview value so a large state subtree cannot flood the response.

    The editor calls the preview endpoint on every pause in typing, so an
    uncapped before/after would ship whole state trees per keystroke.
    """
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        text = str(value)
    if len(text) <= _MAX_PREVIEW_VALUE_CHARS:
        return value
    return {
        "__argus_truncated__": True,
        "preview": text[:_MAX_PREVIEW_VALUE_CHARS],
        "full_length": len(text),
    }


def _serialize_patch_changes(changes: list[Any]) -> list[dict[str, Any]]:
    """Render PatchChange records for the wire, with values capped."""
    return [
        {
            "op": c.op,
            "path": c.path,
            "before": _cap_preview_value(c.before),
            "after": _cap_preview_value(c.after),
            "existed": c.existed,
        }
        for c in changes
    ]


def _run_replay_worker(
    job_id: str,
    run_id: str,
    from_node: str,
    app_module_str: str | None,
    mode: str = "full",
    patch: dict[str, Any] | None = None,
    create_missing: bool = False,
) -> None:
    """Background thread: runs ReplayEngine and updates _replay_jobs on completion.

    The optional *patch* edits the replayed node's recorded input state before
    execution; it is validated by the request handler before this thread starts.
    """
    from argus.replay import ReplayEngine  # noqa: PLC0415

    try:
        engine = ReplayEngine()
        if mode == "node":
            new_run_id = engine.replay_node(
                run_id=run_id,
                node_name=from_node,
                patch=patch,
                create_missing=create_missing,
            )
        else:
            factory = _import_factory_for_ui(app_module_str) if app_module_str else None
            new_run_id = engine.replay(
                run_id=run_id,
                from_node=from_node,
                app_factory=factory,
                patch=patch,
                create_missing=create_missing,
            )
        with _replay_lock:
            _replay_jobs[job_id] = {"status": "done", "run_id": new_run_id, "error": None}
    except Exception as exc:
        error_str = str(exc)
        error_code = "replay_failed"
        if "returned a dict" in error_str or "app_factory must return" in error_str:
            error_code = "bad_factory"
        elif "returned None" in error_str:
            error_code = "bad_factory"
        with _replay_lock:
            _replay_jobs[job_id] = {
                "status": "error",
                "run_id": None,
                "error": error_str,
                "error_code": error_code,
            }


def _all_run_files(project_dir: Path) -> list[Path]:
    """Collect all *.json run files from every .argus/runs/ directory under project_dir."""
    files: list[Path] = []
    for runs_path in project_dir.rglob(".argus/runs"):
        if runs_path.is_dir():
            files.extend(runs_path.glob("*.json"))
    return files


def _all_log_dirs(project_dir: Path) -> list[Path]:
    return [p for p in project_dir.rglob(".argus/logs") if p.is_dir()]


def _make_handler(
    runs_dir: Path,
    logs_dir: Path,
    app_module_str: str | None,
    project_dir: Path | None = None,
) -> type:
    _project_dir = project_dir or runs_dir.parent.parent

    class ArgusHandler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # suppress access logs
            pass

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Access-Control-Allow-Origin", f"http://localhost:{_UI_PORT}")

        def _send_json(self, data: object, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: int = 200) -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            if not path.exists():
                self.send_response(404)
                self.end_headers()
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", _content_type(path.suffix))
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _try_auto_locate(record: object) -> object:
            """Attempt post-hoc source resolution for a run record."""
            try:
                from argus.source_locator import (  # noqa: PLC0415
                    derive_node_fn_refs,
                    locate_node_sources,
                )
                from argus.storage import save_run as _save  # noqa: PLC0415

                resolved = locate_node_sources(record, use_llm=True)
                if resolved:
                    record.node_fn_paths = resolved
                    refs = derive_node_fn_refs(resolved)
                    if refs:
                        record.node_fn_refs = refs
                        _save(record)
            except Exception:
                pass  # best-effort — fall through to original error handling
            return record

        def _list_runs(self) -> None:
            all_files = _all_run_files(_project_dir)
            if not all_files:
                self._send_json([])
                return
            aliases = _load_aliases(_project_dir)
            seen: set[str] = set()
            summaries = []
            for f in all_files:
                try:
                    run = json.loads(f.read_text())
                    rid = run["run_id"]
                    if rid in seen:
                        continue
                    seen.add(rid)
                    summaries.append(
                        {
                            "run_id": rid,
                            "overall_status": run["overall_status"],
                            "started_at": run["started_at"],
                            "duration_ms": run.get("duration_ms"),
                            "step_count": len(run.get("steps", [])),
                            "first_failure_step": run.get("first_failure_step"),
                            "graph_node_names": run.get("graph_node_names", []),
                            "argus_version": run.get("argus_version", ""),
                            "parent_run_id": run.get("parent_run_id"),
                            "replay_from_step": run.get("replay_from_step"),
                            "alias": aliases.get(rid),
                        }
                    )
                except Exception:
                    pass
            summaries.sort(key=lambda r: r["started_at"], reverse=True)
            self._send_json(summaries)

        def _get_run(self, run_id: str) -> None:
            for f in _all_run_files(_project_dir):
                if f.stem == run_id or f.stem.startswith(run_id):
                    try:
                        self._send_json(json.loads(f.read_text()))
                        return
                    except Exception:
                        pass
            self._send_json({"error": "not found"}, 404)

        def _get_fix(
            self, run_id: str, *, node: str | None, sanitized: bool
        ) -> None:
            """Same markdown `argus fix` prints — for the dashboard Copy action."""
            from argus.fix_prompt import (  # noqa: PLC0415
                FixPromptError,
                build_fix_prompt_for_record,
            )
            from argus.storage import _deserialize_run  # noqa: PLC0415

            for f in _all_run_files(_project_dir):
                if f.stem != run_id and not f.stem.startswith(run_id):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    record = _deserialize_run(data)
                except Exception:
                    continue
                try:
                    result = build_fix_prompt_for_record(
                        record, node=node, sanitized=sanitized
                    )
                except FixPromptError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 500)
                    return
                self._send_json(
                    {
                        "run_id": record.run_id,
                        "node": result.node,
                        "source_path": result.source_path,
                        "prompt": result.prompt,
                    }
                )
                return
            self._send_json({"error": "not found"}, 404)

        def _get_run_children(self, run_id: str) -> None:
            from argus.storage import list_replay_children  # noqa: PLC0415

            children = list_replay_children(run_id)
            # Also scan all project run files for cloud-synced children
            for f in _all_run_files(_project_dir):
                try:
                    data = json.loads(f.read_text())
                    if data.get("parent_run_id") == run_id:
                        rid = data.get("run_id", f.stem)
                        if not any(c["run_id"] == rid for c in children):
                            children.append(
                                {
                                    "run_id": rid,
                                    "started_at": data.get("started_at", ""),
                                    "overall_status": data.get("overall_status", "unknown"),
                                    "duration_ms": data.get("duration_ms"),
                                    "step_count": len(data.get("steps", [])),
                                    "replay_from_step": data.get("replay_from_step"),
                                    "parent_run_id": run_id,
                                }
                            )
                except Exception:
                    continue
            children.sort(key=lambda r: r.get("started_at", ""))
            self._send_json(children)

        def _get_run_tree(self, run_id: str) -> None:
            from argus.storage import build_replay_tree  # noqa: PLC0415

            tree = build_replay_tree(run_id)
            self._send_json(tree)

        def _get_log(self, run_id: str) -> None:
            for log_dir in _all_log_dirs(_project_dir):
                for f in log_dir.glob("*.log"):
                    if f.stem == run_id or f.stem.startswith(run_id):
                        try:
                            self._send_text(f.read_text().strip())
                            return
                        except Exception:
                            pass
            self.send_response(404)
            self.end_headers()

        def _compare(self, a: str, b: str) -> None:
            def read_run(run_id: str) -> object:
                for f in _all_run_files(_project_dir):
                    if f.stem == run_id or f.stem.startswith(run_id):
                        try:
                            return json.loads(f.read_text())
                        except Exception:
                            pass
                return None

            self._send_json({"a": read_run(a), "b": read_run(b)})

        def _serve_static(self, path: str) -> None:
            dist = _DIST_DIR.resolve()
            path = unquote(path)  # decode %5B[%5D] → [...] etc.

            # Root
            if path in ("", "/"):
                self._send_file(dist / "index.html")
                return

            # Try exact file first (JS, CSS, images, etc.)
            candidate = (dist / path.lstrip("/")).resolve()
            if not candidate.is_relative_to(dist):
                self.send_response(403)
                self.end_headers()
                return
            if candidate.is_file():
                self._send_file(candidate)
                return

            # Try path/index.html (Next.js trailing-slash pages)
            index = candidate / "index.html"
            if index.resolve().is_relative_to(dist) and index.is_file():
                self._send_file(index)
                return

            # SPA fallback for /settings → serve settings page
            if path.startswith("/settings"):
                settings_page = dist / "settings" / "index.html"
                if settings_page.is_file():
                    self._send_file(settings_page)
                    return

            # SPA fallback for /runs/<any-id> → serve the placeholder shell
            if path.startswith("/runs/"):
                placeholder = dist / "runs" / "_" / "index.html"
                if placeholder.is_file():
                    self._send_file(placeholder)
                    return

            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            if path == "/api/auth":
                origin = self.headers.get("Origin", "")
                if origin and origin != f"http://localhost:{_UI_PORT}":
                    self._send_json({"error": "forbidden"}, 403)
                    return
                auth = _get_cli_auth()
                if auth:
                    self._send_json(auth)
                else:
                    self._send_json({"error": "not logged in"}, 401)
            elif path == "/api/runs":
                self._list_runs()
            elif path.startswith("/api/runs/") and path.endswith("/children"):
                rid = path[len("/api/runs/") : -len("/children")]
                self._get_run_children(rid)
            elif path.startswith("/api/runs/") and path.endswith("/tree"):
                rid = path[len("/api/runs/") : -len("/tree")]
                self._get_run_tree(rid)
            elif path.startswith("/api/runs/") and path.endswith("/fix"):
                rid = unquote(path[len("/api/runs/") : -len("/fix")])
                qs = parse_qs(parsed.query)
                node = (qs.get("node", [None])[0] or None) or None
                sanitized = (qs.get("sanitized", [""])[0] or "").lower() in (
                    "1",
                    "true",
                    "yes",
                )
                self._get_fix(rid, node=node, sanitized=sanitized)
            elif path.startswith("/api/runs/"):
                self._get_run(path[len("/api/runs/") :])
            elif path.startswith("/api/logs/"):
                self._get_log(path[len("/api/logs/") :])
            elif path == "/api/compare":
                qs = parse_qs(parsed.query)
                a = qs.get("a", [""])[0]
                b = qs.get("b", [""])[0]
                self._compare(a, b)
            elif path == "/api/config":
                self._send_json({"app": app_module_str or _load_config_app_factory() or ""})
            elif path == "/api/candidates":
                from argus.candidate_store import load_candidates  # noqa: PLC0415

                data = load_candidates()
                self._send_json(
                    {
                        "candidates": data.get("candidates", []),
                        "rejected_count": len(data.get("rejected_patterns", [])),
                    }
                )
            elif path == "/api/custom-signatures":
                from argus.candidate_store import load_custom_signatures  # noqa: PLC0415

                data = load_custom_signatures()
                self._send_json(data.get("signatures", []))
            elif path == "/api/signature-stats":
                import dataclasses  # noqa: PLC0415

                from argus.signature_stats import compute_stats  # noqa: PLC0415

                all_stats = compute_stats(include_builtins=False)
                self._send_json([dataclasses.asdict(s) for s in all_stats.values()])
            elif path == "/api/shared-signatures":
                from argus.cloud import pull_shared_signatures  # noqa: PLC0415

                sigs = pull_shared_signatures()
                self._send_json(sigs)
            elif path == "/api/shared-signatures/sync":
                from argus.registry import sync_shared_signatures  # noqa: PLC0415

                count = sync_shared_signatures()
                self._send_json({"synced": count})
            elif path == "/api/feedback":
                from argus.feedback_store import load_feedback  # noqa: PLC0415

                data = load_feedback()
                self._send_json(data)
            elif path == "/api/serving":
                self._send_json(
                    {
                        "project_root": str(_project_dir),
                        "runs_dir": str(runs_dir),
                        "cwd": str(Path.cwd()),
                    }
                )
            elif path == "/api/doctor":
                self._send_json(_collect_doctor_info())
            elif path == "/api/settings":
                cfg = _load_config()
                linear_key = cfg.get("linear_api_key", "")
                masked = ("•" * 8 + linear_key[-4:]) if len(linear_key) > 4 else ""
                discord_wh = cfg.get("discord_webhook", "")
                wh_masked = ("•" * 40 + discord_wh[-8:]) if len(discord_wh) > 8 else ""
                self._send_json(
                    {
                        "linear_api_key_set": bool(linear_key),
                        "linear_api_key_masked": masked,
                        "linear_team_id": cfg.get("linear_team_id", ""),
                        "linear_team_name": cfg.get("linear_team_name", ""),
                        "discord_webhook_set": bool(discord_wh),
                        "discord_webhook_masked": wh_masked,
                    }
                )
            elif path == "/api/linear/teams":
                cfg = _load_config()
                api_key = cfg.get("linear_api_key", "")
                if not api_key:
                    self._send_json({"error": "Linear API key not configured"}, 400)
                else:
                    try:
                        teams = _linear_get_teams(api_key)
                        self._send_json({"teams": teams})
                    except Exception as exc:
                        self._send_json({"error": f"Linear API error: {exc}"}, 500)
            elif path == "/api/linear/labels":
                cfg = _load_config()
                api_key = cfg.get("linear_api_key", "")
                team_id = cfg.get("linear_team_id", "")
                if not api_key or not team_id:
                    self._send_json({"error": "Linear not configured"}, 400)
                else:
                    try:
                        labels = _linear_get_labels(api_key, team_id)
                        self._send_json({"labels": labels})
                    except Exception as exc:
                        self._send_json({"error": f"Linear API error: {exc}"}, 500)
            elif path.startswith("/api/replay/status/"):
                job_id = path[len("/api/replay/status/") :]
                with _replay_lock:
                    job = _replay_jobs.get(job_id)
                if job is None:
                    self._send_json({"error": "unknown job"}, 404)
                else:
                    resp: dict = {"status": job["status"]}
                    if job.get("run_id"):
                        resp["run_id"] = job["run_id"]
                    if job.get("error"):
                        resp["message"] = job["error"]
                    if job.get("error_code"):
                        resp["error_code"] = job["error_code"]
                    self._send_json(resp)
            else:
                self._serve_static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            if path == "/api/config":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return
                new_app = (data.get("app") or "").strip()
                if not new_app:
                    self._send_json({"error": "app is required"}, 400)
                    return
                _save_config_app_factory(new_app)
                self._send_json({"app": new_app})
            elif path == "/api/settings":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return

                updates: dict = {}
                if "linear_api_key" in data:
                    updates["linear_api_key"] = (data["linear_api_key"] or "").strip()
                if "linear_team_id" in data:
                    updates["linear_team_id"] = (data["linear_team_id"] or "").strip()
                if "linear_team_name" in data:
                    updates["linear_team_name"] = (data["linear_team_name"] or "").strip()
                if "discord_webhook" in data:
                    wh_val = (data["discord_webhook"] or "").strip()
                    updates["discord_webhook"] = wh_val
                    # Also save to global ~/.argus/config.json
                    try:
                        gc = {}
                        if _GLOBAL_CONFIG.exists():
                            gc = json.loads(_GLOBAL_CONFIG.read_text())
                        gc["discord_webhook"] = wh_val
                        _GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                        _GLOBAL_CONFIG.write_text(json.dumps(gc, indent=2))
                        _GLOBAL_CONFIG.chmod(0o600)
                    except Exception:
                        pass
                if not updates:
                    self._send_json({"error": "no settings provided"}, 400)
                    return
                _save_config(updates)
                self._send_json({"ok": True})
            elif path == "/api/compare-analysis":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return

                id_a = (data.get("a") or "").strip()
                id_b = (data.get("b") or "").strip()
                if not id_a or not id_b:
                    self._send_json({"error": "a and b run IDs are required"}, 400)
                    return

                from argus.storage import load_run as _load_run  # noqa: PLC0415

                try:
                    rec_a = _load_run(id_a)
                except (FileNotFoundError, ValueError):
                    self._send_json({"error": f"run '{id_a}' not found"}, 404)
                    return
                try:
                    rec_b = _load_run(id_b)
                except (FileNotFoundError, ValueError):
                    self._send_json({"error": f"run '{id_b}' not found"}, 404)
                    return

                try:
                    from argus.llm_investigator import compare_runs  # noqa: PLC0415

                    result = compare_runs(rec_a, rec_b)
                    self._send_json(result)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 500)
            elif path.startswith("/api/runs/") and path.endswith("/locate"):
                rid = path[len("/api/runs/") : -len("/locate")]
                try:
                    from argus.source_locator import (  # noqa: PLC0415
                        derive_node_fn_refs,
                        locate_node_sources,
                    )
                    from argus.storage import load_run as _lr  # noqa: PLC0415
                    from argus.storage import save_run as _sr  # noqa: PLC0415

                    rec = _lr(rid)
                    resolved = locate_node_sources(rec, use_llm=True)
                    if resolved:
                        rec.node_fn_paths = resolved
                        refs = derive_node_fn_refs(resolved)
                        if refs:
                            rec.node_fn_refs = refs
                        _sr(rec)
                    self._send_json({"node_fn_paths": resolved or {}})
                except FileNotFoundError:
                    self._send_json({"error": "run not found"}, 404)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 500)
            elif path == "/api/replay":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return

                run_id = (data.get("run_id") or "").strip()
                from_step = (data.get("from_step") or "").strip()
                replay_mode = (data.get("mode") or "full").strip()

                if not run_id or not from_step:
                    self._send_json({"error": "run_id and from_step are required"}, 400)
                    return

                # Load the run to check existence and stored node refs
                from argus.storage import load_run as _load_run  # noqa: PLC0415

                try:
                    run_record = _load_run(run_id)
                except (FileNotFoundError, ValueError):
                    self._send_json({"error": "run not found"}, 404)
                    return

                # Single-node replay only needs node_fn_refs
                effective_app: str | None = None

                # Auto-locate source files if refs are missing
                if not run_record.node_fn_refs:
                    run_record = self._try_auto_locate(run_record)

                if replay_mode == "node":
                    if not run_record.node_fn_refs or from_step not in run_record.node_fn_refs:
                        self._send_json(
                            {
                                "error": "no_node_ref",
                                "message": f"No stored function ref for '{from_step}'. Re-record with latest argus.",  # noqa: E501
                            },
                            422,
                        )
                        return
                else:
                    # Full replay: check factory requirements
                    has_node_refs = bool(run_record.node_fn_refs)
                    if not has_node_refs:
                        effective_app = (
                            app_module_str
                            or _load_config_app_factory()
                            or run_record.app_factory_ref
                        )
                        if not effective_app:
                            self._send_json(
                                {
                                    "error": "no_app_factory",
                                    "message": "Set your app factory in the UI or run argus ui --app module:fn",  # noqa: E501
                                },
                                422,
                            )
                            return

                # Validate the optional state patch up front. A patch that fails
                # inside the worker thread would surface only as a generic job
                # error, losing the "did you mean" hint that makes it fixable.
                patch = data.get("patch")
                create_missing = bool(data.get("create_missing", False))
                if patch is not None:
                    try:
                        _resolve_patch_preview(
                            run_record, from_step, patch, create_missing
                        )
                    except _PatchRequestError as exc:
                        err_body, err_status = exc.as_response()
                        self._send_json(err_body, err_status)
                        return

                job_id = str(uuid.uuid4())
                with _replay_lock:
                    _replay_jobs[job_id] = {"status": "running", "run_id": None, "error": None}

                t = threading.Thread(
                    target=_run_replay_worker,
                    args=(
                        job_id,
                        run_id,
                        from_step,
                        effective_app,
                        replay_mode,
                        patch,
                        create_missing,
                    ),
                    daemon=True,
                )
                t.start()

                self._send_json({"job_id": job_id}, 202)
            elif path == "/api/replay/preview":
                # Answers "what would this patch change?" without running
                # anything — no job is started and no run is written.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return

                run_id = (data.get("run_id") or "").strip()
                from_step = (data.get("from_step") or "").strip()
                if not run_id or not from_step:
                    self._send_json({"error": "run_id and from_step are required"}, 400)
                    return

                if "patch" not in data:
                    self._send_json({"error": "patch is required"}, 400)
                    return

                from argus.storage import load_run as _load_run  # noqa: PLC0415

                try:
                    run_record = _load_run(run_id)
                except (FileNotFoundError, ValueError):
                    self._send_json({"error": "run not found"}, 404)
                    return

                try:
                    changes = _resolve_patch_preview(
                        run_record,
                        from_step,
                        data.get("patch"),
                        bool(data.get("create_missing", False)),
                    )
                except _PatchRequestError as exc:
                    err_body, err_status = exc.as_response()
                    self._send_json(err_body, err_status)
                    return

                from argus.state_patch import format_changes  # noqa: PLC0415

                self._send_json(
                    {
                        "changes": _serialize_patch_changes(changes),
                        "summary": format_changes(changes),
                    }
                )
            elif path.startswith("/api/feedback/") and path.endswith("/resolve"):
                fb_id = path[len("/api/feedback/") : -len("/resolve")]
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return
                verdict = data.get("verdict", "")
                share = data.get("share", False)
                if verdict not in ("agree", "disagree"):
                    self._send_json({"error": "verdict must be 'agree' or 'disagree'"}, 400)
                    return
                from argus.feedback_store import resolve_feedback  # noqa: PLC0415

                result = resolve_feedback(fb_id, verdict, share=share)
                if result is None:
                    self._send_json({"error": "feedback not found"}, 404)
                else:
                    self._send_json(result)
            elif path.startswith("/api/feedback/") and path.endswith("/dismiss"):
                fb_id = path[len("/api/feedback/") : -len("/dismiss")]
                from argus.feedback_store import dismiss_feedback  # noqa: PLC0415

                if dismiss_feedback(fb_id):
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "feedback not found"}, 404)
            elif path == "/api/send-report":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return

                category = (data.get("category") or "").strip()
                description = (data.get("description") or "").strip()
                run_id = (data.get("run_id") or "").strip() or None
                include_run = data.get("include_run", False)

                if not category or not description:
                    self._send_json({"error": "category and description are required"}, 400)
                    return

                # Collect system diagnostics
                system_info = _collect_doctor_info()

                # Collect sanitized run diagnostics if requested
                run_diagnostics = None
                if run_id and include_run:
                    for f in _all_run_files(_project_dir):
                        if f.stem == run_id or f.stem.startswith(run_id):
                            try:
                                run_data = json.loads(f.read_text())
                                run_diagnostics = _sanitize_run_for_report(run_data)
                            except Exception:
                                pass
                            break

                report_id = str(uuid.uuid4())
                report_payload = {
                    "id": report_id,
                    "category": category,
                    "description": description,
                    "system_info": system_info,
                    "run_diagnostics": run_diagnostics,
                    "argus_version": system_info.get("argus_version", ""),
                    "created_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                }

                # Try Supabase upload (non-blocking)
                supabase_ok = False
                try:
                    from argus.cloud import SUPABASE_URL as _sb_url

                    if _sb_url is None:
                        pass  # local-only mode: report upload is hosted-only
                    else:
                        from argus.cloud import (  # noqa: PLC0415
                            SUPABASE_ANON_KEY,
                            SUPABASE_URL,
                            _get_valid_credentials,
                            _supabase_request,
                        )

                        creds = _get_valid_credentials()
                        if creds:
                            report_payload["user_id"] = creds.user_id
                            _supabase_request(
                                creds.access_token,
                                "reports",
                                method="POST",
                                body=report_payload,
                                extra_headers={"Prefer": "return=minimal"},
                            )
                            supabase_ok = True
                        else:
                            # Fallback: use anon key so reports work without login
                            import urllib.request as _ur  # noqa: PLC0415

                            _anon_body = json.dumps(report_payload).encode()
                            _anon_req = _ur.Request(
                                f"{SUPABASE_URL}/rest/v1/reports",
                                data=_anon_body,
                                headers={
                                    "Content-Type": "application/json",
                                    "apikey": SUPABASE_ANON_KEY,
                                    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                                    "Prefer": "return=minimal",
                                },
                                method="POST",
                            )
                            _ur.urlopen(_anon_req, timeout=5)
                            supabase_ok = True
                except Exception:
                    pass  # Supabase upload is best-effort

                # Discord webhook notification — primary delivery channel
                discord_ok = False
                discord_err = None
                try:
                    _webhook_url = _get_discord_webhook()
                    if not _webhook_url:
                        raise _SkipWebhook  # noqa: TRY301
                    import urllib.request  # noqa: PLC0415

                    emoji = {
                        "bug": "\U0001f41b",
                        "setup_issue": "\U0001f527",
                        "unexpected_result": "\U0001f914",
                    }
                    icon = emoji.get(category, "\U0001f4cb")
                    title = icon + " Diagnostic Report: " + category
                    ver = system_info.get("argus_version", "?")
                    py_info = system_info.get("python", {})
                    lg_info = system_info.get("langgraph", {})
                    stor_info = system_info.get("storage", {})
                    replay_info = system_info.get("replay", {})
                    deps_info = system_info.get("optional_deps", {})

                    # Build full system info block
                    sys_lines = [
                        f"Python: {py_info.get('message', '?')}",
                        f"LangGraph: {lg_info.get('message', '?')}",
                        f"Storage: {stor_info.get('message', '?')}",
                        f"Replay: {replay_info.get('message', '?')}",
                        f"Deps: {deps_info.get('message', '?')}",
                    ]

                    embed = {
                        "title": title,
                        "description": description[:1500],
                        "color": 0xEF4444 if category == "bug" else 0xF59E0B,
                        "fields": [
                            {
                                "name": "ARGUS Version",
                                "value": ver,
                                "inline": True,
                            },
                            {
                                "name": "Category",
                                "value": category,
                                "inline": True,
                            },
                            {
                                "name": "System Info",
                                "value": "```\n" + "\n".join(sys_lines) + "\n```",
                                "inline": False,
                            },
                        ],
                        "footer": {"text": "ARGUS Diagnostic Report"},
                    }
                    if run_diagnostics:
                        rid = run_diagnostics.get("run_id", "?")
                        st = run_diagnostics.get("overall_status", "?")
                        dur = run_diagnostics.get("duration_ms", "?")
                        nodes = run_diagnostics.get("graph_node_names", [])
                        rcc = run_diagnostics.get("root_cause_chain", [])
                        steps = run_diagnostics.get("steps", [])
                        edges = run_diagnostics.get("graph_edge_map", {})

                        # ── Run overview ──
                        status_emoji = {
                            "clean": "\u2705",
                            "silent_failure": "\U0001f7e1",
                            "crashed": "\u274c",
                            "semantic_fail": "\U0001f7e3",
                            "interrupted": "\u23f8\ufe0f",
                        }
                        se = status_emoji.get(st, "\u2753")
                        run_hdr = f"{se} **{st}**"
                        run_hdr += f" | {dur}ms"
                        run_hdr += f" | {len(steps)} steps\n"
                        topo = " \u2192 ".join(nodes)
                        run_hdr += f"```{topo}```\n"
                        if rcc:
                            run_hdr += "\U0001f50d **Root cause:** " + " \u2192 ".join(rcc) + "\n"
                        embed["fields"].append(
                            {
                                "name": f"Run: {rid}",
                                "value": run_hdr[:1024],
                                "inline": False,
                            }
                        )

                        # ── Per-step detail ──
                        step_blocks = []
                        for s in steps[:10]:
                            sn = s.get("node_name", "?")
                            ss = s.get("status", "?")
                            sd = s.get("duration_ms", 0)
                            se2 = status_emoji.get(ss, "\u25cf")
                            ik = s.get("input_keys", [])
                            ok_ = s.get("output_keys", [])
                            line = f"{se2} **{sn}** ({ss}, {sd}ms)"
                            if ik or ok_:
                                line += f"\n  in:`{ik}` out:`{ok_}`"
                            insp = s.get("inspection", {})
                            # Tool failures
                            for tf in insp.get("tool_failures", []):
                                ft = tf.get("failure_type", "?")
                                sv = tf.get("severity", "?")
                                ev = str(tf.get("evidence", ""))
                                ico = "\U0001f534" if sv == "critical" else "\U0001f7e0"
                                line += f"\n  {ico} `{ft}` ({sv})"
                                if ev:
                                    line += f" {ev[:80]}"
                            # Semantic signals
                            for ss2 in insp.get("semantic_signals", []):
                                sid = ss2.get("sig_id", "?")
                                scat = ss2.get("category", "?")
                                line += f"\n  \U0001f7e3 `{sid}` [{scat}]"
                            # Missing / empty fields
                            mf = insp.get("missing_fields", [])
                            if mf:
                                line += "\n  \u26a0\ufe0f missing: "
                                line += ", ".join(f"`{f}`" for f in mf[:5])
                            ef = insp.get("empty_fields", [])
                            if ef:
                                line += "\n  \u26a0\ufe0f empty: "
                                line += ", ".join(f"`{f}`" for f in ef[:5])
                            # Anomaly signals
                            for a in s.get("anomaly_signals", []):
                                aid = a.get("anomaly_id", "?")
                                ar = str(a.get("reason", ""))
                                asc = a.get("suspicion_score")
                                asv = a.get("severity", "?")
                                ico = "\U0001f534" if asv == "critical" else "\U0001f7e0"
                                sc_txt = f" ({asc})" if asc is not None else ""
                                line += f"\n  {ico} [{aid}]{sc_txt} {ar[:60]}"
                            # Semantic check (LLM judge)
                            sc = s.get("semantic_check")
                            if sc:
                                sp = "\u2705" if sc.get("passed") else "\u274c"
                                scf = sc.get("confidence", "?")
                                scr = str(sc.get("reason", ""))
                                line += f"\n  {sp} LLM judge: {scf} — {scr[:80]}"
                            # Validator results
                            for v in s.get("validator_results", []):
                                vp = "\u2705" if v.get("is_valid") else "\u274c"
                                vn = v.get("validator_name", "?")
                                vm = str(v.get("message", ""))
                                line += f"\n  {vp} validator `{vn}` {vm[:60]}"
                            # Exception
                            exc = s.get("exception")
                            if exc:
                                exc = str(exc)
                                line += f"\n  \U0001f4a5 ```{exc[:120]}```"
                            step_blocks.append(line)

                        if step_blocks:
                            # Discord field value max 1024
                            val = "\n".join(step_blocks)
                            embed["fields"].append(
                                {
                                    "name": "Steps",
                                    "value": val[:1024],
                                    "inline": False,
                                }
                            )

                        # ── LLM investigation ──
                        llm_inv = run_diagnostics.get("llm_investigation", {})
                        if llm_inv and llm_inv.get("triggered"):
                            inv_lines = []
                            rc_expl = llm_inv.get("root_cause_explanation", "")
                            if rc_expl:
                                inv_lines.append(rc_expl[:400])
                            triggers = llm_inv.get("trigger_reasons", [])
                            if triggers:
                                inv_lines.append("**Triggers:** " + ", ".join(triggers))
                            conf = llm_inv.get("confidence")
                            if conf is not None:
                                inv_lines.append(f"**Confidence:** {conf}")
                            dbg = llm_inv.get("debugging_suggestions", [])
                            if dbg:
                                inv_lines.append(
                                    "**Debug:**\n" + "\n".join(f"• {d[:80]}" for d in dbg[:4])
                                )
                            if inv_lines:
                                embed["fields"].append(
                                    {
                                        "name": "\U0001f9e0 LLM Investigation",
                                        "value": "\n".join(inv_lines)[:1024],
                                        "inline": False,
                                    }
                                )

                        # ── Correlation analysis ──
                        corr = run_diagnostics.get("correlation", {})
                        if corr:
                            cl = []
                            cs = corr.get("causal_summary")
                            if cs:
                                cl.append(cs[:300])
                            do = corr.get("degradation_origins", [])
                            if do:
                                cl.append("**Origins:** " + ", ".join(str(o) for o in do[:5]))
                            pc = corr.get("propagation_chains", [])
                            if pc:
                                for ch in pc[:3]:
                                    cl.append("\u26d3 " + str(ch)[:100])
                            cli2 = corr.get("llm_insight")
                            if cli2:
                                cl.append(f"**LLM:** {cli2[:200]}")
                            if cl:
                                embed["fields"].append(
                                    {
                                        "name": ("\U0001f517 Correlation"),
                                        "value": "\n".join(cl)[:1024],
                                        "inline": False,
                                    }
                                )

                        # ── Edge map ──
                        if edges:
                            edge_lines = []
                            for src, tgts in edges.items():
                                for t in tgts:
                                    edge_lines.append(f"{src} \u2192 {t}")
                            if edge_lines:
                                embed["fields"].append(
                                    {
                                        "name": "Graph Edges",
                                        "value": "```\n" + "\n".join(edge_lines[:15]) + "\n```",
                                        "inline": False,
                                    }
                                )

                    webhook_body = json.dumps({"embeds": [embed]}).encode()
                    req = urllib.request.Request(
                        _webhook_url,
                        data=webhook_body,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "ARGUS-Diagnostic/1.0",
                        },
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=5)
                    discord_ok = True
                except _SkipWebhook:
                    discord_err = "no webhook configured"
                except Exception as exc:
                    discord_err = str(exc)
                    print(f"[ARGUS] Discord webhook error: {exc}")  # noqa: T201

                # Linear issue creation (if configured and requested)
                linear_result = None
                send_to_linear = data.get("send_to_linear", False)
                if send_to_linear:
                    try:
                        cfg = _load_config()
                        lin_key = cfg.get("linear_api_key", "")
                        lin_team = cfg.get("linear_team_id", "")
                        if lin_key and lin_team:
                            emoji = {
                                "bug": "\U0001f41b",
                                "setup_issue": "\U0001f527",
                                "unexpected_result": "\U0001f914",
                                "feature": "\u2728",
                                "improvement": "\U0001f4a1",
                            }
                            icon = emoji.get(category, "\U0001f4cb")
                            cat_label = category.replace("_", " ").title()
                            desc_short = description[:80]
                            lin_title = f"{icon} [{cat_label}] {desc_short}"

                            # Build markdown body with full diagnostics
                            lin_body = f"## Description\n\n{description}\n\n"
                            lin_body += f"**Category:** {category}\n"
                            ver = system_info.get("argus_version", "?")
                            lin_body += f"**ARGUS Version:** {ver}\n"
                            if run_diagnostics:
                                rid = run_diagnostics.get("run_id", "?")
                                st = run_diagnostics.get("overall_status", "?")
                                dur = run_diagnostics.get("duration_ms")
                                rcc = run_diagnostics.get("root_cause_chain", [])
                                nodes = run_diagnostics.get("graph_node_names", [])
                                edges = run_diagnostics.get("graph_edge_map", {})
                                is_cyclic = run_diagnostics.get("is_cyclic", False)
                                first_fail = run_diagnostics.get("first_failure_step")

                                lin_body += "\n## Run Diagnostics\n\n"
                                lin_body += f"- **Run ID:** `{rid}`\n"
                                lin_body += f"- **Status:** {st}\n"
                                if dur is not None:
                                    lin_body += f"- **Duration:** {dur}ms\n"
                                if first_fail:
                                    lin_body += f"- **First failure:** `{first_fail}`\n"
                                if rcc:
                                    lin_body += f"- **Root cause chain:** {' → '.join(rcc)}\n"
                                if nodes:
                                    node_list = ", ".join(f"`{n}`" for n in nodes)
                                    lin_body += f"- **Graph nodes:** {node_list}\n"
                                if is_cyclic:
                                    lin_body += "- **Cyclic graph:** yes\n"
                                if edges:
                                    edge_strs = [
                                        f"`{s}` → `{', '.join(ds)}`" for s, ds in edges.items()
                                    ]
                                    lin_body += f"- **Edges:** {'; '.join(edge_strs)}\n"

                                steps = run_diagnostics.get("steps", [])
                                if steps:
                                    lin_body += f"\n### Steps ({len(steps)})\n\n"
                                    for s in steps:
                                        sn = s.get("node_name", "?")
                                        ss = s.get("status", "?")
                                        sd = s.get("duration_ms")
                                        bt = s.get("behavior_type")
                                        dur_str = f" ({sd}ms)" if sd is not None else ""
                                        bt_str = f" [{bt}]" if bt else ""
                                        lin_body += f"#### `{sn}`: {ss}{dur_str}{bt_str}\n\n"

                                        # Inspection message
                                        insp_msg = s.get("inspection_message")
                                        if insp_msg and insp_msg != "All checks passed":
                                            lin_body += f"> {insp_msg}\n\n"

                                        # Missing fields
                                        mf = s.get("missing_fields", [])
                                        if mf:
                                            mf_list = ", ".join(f"`{f}`" for f in mf)
                                            lin_body += f"- **Missing fields:** {mf_list}\n"

                                        # Tool failures
                                        tfs = s.get("tool_failures", [])
                                        if tfs:
                                            for tf in tfs:
                                                ft = tf.get("failure_type", "?")
                                                sev = tf.get("severity", "?")
                                                lin_body += f"- **Tool failure:** {ft} ({sev})\n"

                                        # Semantic check
                                        sc = s.get("semantic_check")
                                        if sc:
                                            passed = "PASS" if sc.get("passed") else "FAIL"
                                            conf = sc.get("confidence", "?")
                                            reason = sc.get("reason", "")
                                            lin_body += (
                                                f"- **Semantic check:** {passed}"
                                                f" (confidence: {conf})"
                                            )
                                            if reason:
                                                lin_body += f" — {reason}"
                                            lin_body += "\n"

                                        # Anomaly signals
                                        anomalies = s.get("anomaly_signals", [])
                                        if anomalies:
                                            for a in anomalies:
                                                aid = a.get("anomaly_id", "?")
                                                asev = a.get("severity", "?")
                                                areason = a.get("reason", "")
                                                lin_body += (
                                                    f"- **Anomaly [{aid}]:** {areason} ({asev})\n"
                                                )

                                        # Exception
                                        exc = s.get("exception")
                                        if exc:
                                            exc_short = exc[:500]
                                            lin_body += (
                                                f"- **Exception:**\n```\n{exc_short}\n```\n"
                                            )

                                        lin_body += "\n"

                                # LLM investigation
                                llm_inv = run_diagnostics.get("llm_investigation", {})
                                if llm_inv and llm_inv.get("triggered"):
                                    lin_body += "### LLM Investigation\n\n"
                                    rc_expl = llm_inv.get("root_cause_explanation", "")
                                    rc_node = llm_inv.get("root_cause_node", "")
                                    conf = llm_inv.get("confidence", "?")
                                    triggers = llm_inv.get("trigger_reasons", [])
                                    if rc_node:
                                        lin_body += f"- **Root cause node:** `{rc_node}`\n"
                                    if rc_expl:
                                        lin_body += f"- **Explanation:** {rc_expl}\n"
                                    lin_body += f"- **Confidence:** {conf}\n"
                                    if triggers:
                                        trig_list = ", ".join(triggers)
                                        lin_body += f"- **Trigger reasons:** {trig_list}\n"

                            linear_result = _send_to_linear(
                                lin_key,
                                lin_team,
                                category,
                                lin_title,
                                lin_body,
                            )
                    except Exception:
                        pass  # Linear is best-effort

                resp: dict = {
                    "ok": True,
                    "report_id": report_id,
                    "cloud_synced": supabase_ok,
                    "discord": discord_ok,
                }
                if discord_err:
                    resp["discord_error"] = discord_err
                if linear_result:
                    resp["linear"] = linear_result
                self._send_json(resp)
            elif path.startswith("/api/candidates/") and path.endswith("/approve-shared"):
                cand_id = path[len("/api/candidates/") : -len("/approve-shared")]
                from argus.candidate_store import approve_candidate_shared  # noqa: PLC0415
                from argus.registry import reload_registry, sync_shared_signatures  # noqa: PLC0415

                result = approve_candidate_shared(cand_id)
                if result is None:
                    self._send_json({"error": "candidate not found or not logged in"}, 400)
                else:
                    sync_shared_signatures()
                    reload_registry()
                    self._send_json(result)
            elif path.startswith("/api/candidates/") and path.endswith("/approve"):
                cand_id = path[len("/api/candidates/") : -len("/approve")]
                from argus.candidate_store import approve_candidate  # noqa: PLC0415
                from argus.registry import reload_registry  # noqa: PLC0415

                result = approve_candidate(cand_id)
                if result is None:
                    self._send_json({"error": "candidate not found"}, 404)
                else:
                    reload_registry()
                    self._send_json(result)
            elif path.startswith("/api/candidates/") and path.endswith("/reject"):
                cand_id = path[len("/api/candidates/") : -len("/reject")]
                from argus.candidate_store import reject_candidate  # noqa: PLC0415

                if reject_candidate(cand_id):
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "candidate not found"}, 404)
            elif path.startswith("/api/custom-signatures/") and path.endswith("/disable"):
                sig_id = path[len("/api/custom-signatures/") : -len("/disable")]
                from argus.candidate_store import disable_custom_signature  # noqa: PLC0415
                from argus.registry import reload_registry  # noqa: PLC0415

                if disable_custom_signature(sig_id):
                    reload_registry()
                    self._send_json({"ok": True, "sig_id": sig_id, "disabled": True})
                else:
                    self._send_json({"error": "signature not found"}, 404)
            elif path.startswith("/api/custom-signatures/") and path.endswith("/enable"):
                sig_id = path[len("/api/custom-signatures/") : -len("/enable")]
                from argus.candidate_store import enable_custom_signature  # noqa: PLC0415
                from argus.registry import reload_registry  # noqa: PLC0415

                if enable_custom_signature(sig_id):
                    reload_registry()
                    self._send_json({"ok": True, "sig_id": sig_id, "disabled": False})
                else:
                    self._send_json({"error": "signature not found"}, 404)
            elif path == "/api/signature-stats/dispute":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return
                from argus.signature_stats import record_dispute  # noqa: PLC0415

                dispute_id = record_dispute(
                    sig_id=data.get("sig_id", ""),
                    run_id=data.get("run_id", ""),
                    node_name=data.get("node_name", ""),
                    field_path=data.get("field_path", ""),
                    evidence=data.get("evidence", ""),
                    reason=data.get("reason", ""),
                )
                self._send_json({"ok": True, "dispute_id": dispute_id})
            else:
                self._send_json({"error": "not found"}, 404)

        def do_PUT(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            if path.startswith("/api/runs/") and path.endswith("/alias"):
                rid = path[len("/api/runs/") : -len("/alias")]
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self._send_json({"error": "invalid JSON"}, 400)
                    return
                alias = (data.get("alias") or "").strip()
                if not alias:
                    self._send_json({"error": "alias is required"}, 400)
                    return
                aliases = _load_aliases(_project_dir)
                aliases[rid] = alias
                _save_aliases(aliases, _project_dir)
                self._send_json({"run_id": rid, "alias": alias})
            else:
                self._send_json({"error": "not found"}, 404)

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            if path.startswith("/api/runs/") and path.endswith("/alias"):
                rid = path[len("/api/runs/") : -len("/alias")]
                aliases = _load_aliases(_project_dir)
                aliases.pop(rid, None)
                _save_aliases(aliases, _project_dir)
                self._send_json({"run_id": rid, "alias": None})
            elif path.startswith("/api/custom-signatures/"):
                sig_id = path[len("/api/custom-signatures/") :]
                from argus.candidate_store import delete_custom_signature  # noqa: PLC0415
                from argus.registry import reload_registry  # noqa: PLC0415

                if delete_custom_signature(sig_id):
                    reload_registry()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "not found"}, 404)
            elif path.startswith("/api/runs/"):
                rid = path[len("/api/runs/") :]
                deleted = False
                for f in _all_run_files(_project_dir):
                    if f.stem == rid or f.stem.startswith(rid):
                        f.unlink()
                        deleted = True
                        break
                if deleted:
                    # Also remove alias if one exists
                    aliases = _load_aliases(_project_dir)
                    if rid in aliases:
                        aliases.pop(rid)
                        _save_aliases(aliases, _project_dir)
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "not found"}, 404)
            else:
                self._send_json({"error": "not found"}, 404)

    return ArgusHandler


def open_ui(app_module_str: str | None = None) -> None:
    if not _DIST_DIR.is_dir():
        _console.print(
            "[red]Error:[/red] UI assets not found.\n"
            "Run [bold]scripts/build_ui.sh[/bold] to build them."
        )
        raise SystemExit(1)

    project_dir = resolve_project_root()
    runs_dir = resolved_runs_dir(create=False)
    logs_dir = argus_dir() / "logs"

    if _port_in_use():
        remote = probe_running_ui()
        for line in format_port_busy_lines(runs_dir, remote):
            if line.startswith("warning:"):
                _console.print(f"  [yellow]{line}[/yellow]")
            elif line.startswith("Argus UI already"):
                _console.print(f"Argus UI already running at [bold]{_UI_URL}[/bold]")
            else:
                _console.print(f"  [dim]{line}[/dim]")
        webbrowser.open(_UI_URL)
        return

    # Resolve factory: --app flag → .argus/config.json → None
    effective = app_module_str or _load_config_app_factory()

    handler = _make_handler(runs_dir, logs_dir, effective, project_dir)
    server = ThreadingHTTPServer(("localhost", _UI_PORT), handler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    run_count = count_run_json_files(runs_dir)
    for line in format_ui_startup_lines(runs_dir, run_count):
        if line.startswith("warning:"):
            _console.print(f"  [yellow]{line}[/yellow]")
        elif line.startswith("Argus UI running"):
            _console.print(f"Argus UI running at [bold]{_UI_URL}[/bold]")
        elif line.startswith("serving runs from"):
            _console.print(f"  [dim]serving runs from[/dim] {runs_dir}")
        else:
            _console.print(f"  {line}")
    _console.print("  [dim]replay[/dim] [bold]auto-detect[/bold] [dim](zero-config)[/dim]")
    webbrowser.open(_UI_URL)

    # Keep the process alive — join with timeout so Ctrl+C is not swallowed
    _console.print("  [dim]press Ctrl+C to stop[/dim]")
    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        _console.print("\n[dim]Shutting down…[/dim]")
        server.shutdown()
