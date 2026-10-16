"""Shape card: the only thing automatic telemetry sends about a run.

Built by allowlist. Every field below is named on purpose; nothing is copied
from the run wholesale, so there is no code path that can carry a value out.

- Names a pipeline chose (nodes, state keys, tools, field paths) become a keyed
  hash, ``k_<10 hex>``. The key is a per-install secret that never leaves the
  machine, so the same node hashes the same way across one install's runs
  (fingerprints and follow-ups line up) and cannot be guessed back from a list
  of likely names.
- Values become ``{t, len, f}``: type, length, and features from a fixed
  vocabulary computed here (``done_word``, ``error_word``, …). No substring.
- Rule names are code constants, but a learned-signature category or an LLM
  review kind is free text, so anything that is not a short snake_case word
  becomes ``other``.

The published contract is ``argus/data/share_card.schema.json``; the intake
rejects any field it does not list. See docs/DATA.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
from importlib import metadata
from typing import Any

from argus.models import Finding, NodeEvent, RunRecord
from argus.review import is_heuristic

SCHEMA_VERSION = 1
MAX_DEPTH = 6
MAX_KEYS = 50
MAX_STEPS = 200
MAX_FINDINGS = 200
MAX_TOOLS = 50

_WORD = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_DONE = re.compile(
    r"\b(done|completed?|succeeded|success(ful)?|scheduled|approved|sent|processed|"
    r"created|confirmed|refunded|resolved)\b"
)
_PENDING = re.compile(r"\b(pending\w*|requires_approval|awaiting|queued|in_progress|on_hold)\b")
_ERROR = re.compile(
    r"\b(error|failed|failure|exception|denied|forbidden|unauthorized|timed? ?out|"
    r"not found|invalid)\b"
)
_REFUSAL = re.compile(r"\b(i can'?t|i cannot|i'?m unable|i am unable|as an ai)\b")
_PLACEHOLDER = re.compile(r"(lorem ipsum|\btodo\b|\btbd\b|\[insert|placeholder|^n/?a$)")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")


def _h(name: str, secret: str, prefix: str = "k_") -> str:
    digest = hmac.new(secret.encode(), str(name).encode(), hashlib.sha256).hexdigest()
    return prefix + digest[:10]


def _word(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _WORD.match(text) else "other"


def _features(text: str) -> list[str]:
    low = text.strip().lower()
    if not low:
        return ["empty"]
    checks = (
        ("multiline", "\n" in low),
        ("json_like", _is_json(low)),
        ("url", low.startswith(("http://", "https://"))),
        ("email_like", bool(_EMAIL.match(low))),
        ("numeric", _is_number(low)),
        ("done_word", bool(_DONE.search(low))),
        ("pending_word", bool(_PENDING.search(low))),
        ("error_word", bool(_ERROR.search(low))),
        ("refusal", bool(_REFUSAL.search(low))),
        ("placeholder", bool(_PLACEHOLDER.search(low))),
    )
    return [name for name, hit in checks if hit]


def _is_json(text: str) -> bool:
    if text[:1] not in "[{":
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def shape(value: Any, secret: str, depth: int = 0) -> dict[str, Any]:
    """Type, length and features of ``value``; keys hashed, nothing copied."""
    if depth >= MAX_DEPTH:
        return {"t": "truncated"}
    if value is None:
        return {"t": "null"}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int"}
    if isinstance(value, float):
        return {"t": "float"}
    if isinstance(value, str):
        out: dict[str, Any] = {"t": "string", "len": len(value)}
        features = _features(value)
        return {**out, "f": features} if features else out
    if isinstance(value, (list, tuple)):
        out = {"t": "array", "len": len(value)}
        return {**out, "item": shape(value[0], secret, depth + 1)} if value else out
    if isinstance(value, dict):
        items = list(value.items())
        out = {
            "t": "object",
            "keys": {_h(k, secret): shape(v, secret, depth + 1) for k, v in items[:MAX_KEYS]},
        }
        return {**out, "more": len(items) - MAX_KEYS} if len(items) > MAX_KEYS else out
    return {"t": "other"}


def _field(path: str | None, secret: str) -> str | None:
    if not path:
        return None
    return ".".join(_h(part, secret) for part in path.split(".")[:8])


def _tool(call: dict[str, Any], secret: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": _h(call.get("name") or "", secret),
        "error": bool(call.get("error")),
    }
    return {**out, "output": shape(call["output"], secret)} if "output" in call else out


def _step(event: NodeEvent, secret: str) -> dict[str, Any]:
    return {
        "node": _h(event.node_name, secret),
        "status": _word(event.status),
        "attempt": max(int(event.attempt_index or 0), 0),
        "ms": max(int(event.duration_ms or 0), 0),
        "error": event.exception is not None,
        "update": None if event.output_dict is None else shape(event.output_dict, secret),
        "tools": [_tool(c, secret) for c in event.tool_calls[:MAX_TOOLS]],
        "review": [
            {"role": _word(item.get("role")), "kind": _word(item.get("kind"))}
            for item in event.review[:20]
        ],
    }


def _finding(finding: Finding, secret: str) -> dict[str, Any]:
    return {
        "type": _word(finding.type),
        "severity": _word(finding.severity),
        "source": _word(finding.source),
        "node": _h(finding.node, secret),
        "origin": _h(finding.origin_node, secret) if finding.origin_node else None,
        "field": _field(finding.field_path, secret),
        "heuristic": is_heuristic(finding.type),
        "confidence": finding.confidence,
        "suppressed": bool(finding.suppressed),
    }


def _fingerprint(record: RunRecord, secret: str) -> str:
    nodes = sorted(_h(n, secret) for n in record.graph_node_names)
    edges = sorted(
        f"{_h(src, secret)}>{_h(dst, secret)}"
        for src, dsts in record.graph_edge_map.items()
        for dst in dsts
    )
    return _h("|".join(nodes + edges), secret, prefix="g_")


def _langgraph_version() -> str | None:
    try:
        return metadata.version("langgraph")
    except metadata.PackageNotFoundError:
        return None


def build_card(
    record: RunRecord, *, secret: str, install_id: str, ci: bool = False
) -> dict[str, Any]:
    """The shape card for ``record``. Pure: no I/O beyond reading package versions."""
    edges = sum(len(dsts) for dsts in record.graph_edge_map.values())
    return {
        "schema": SCHEMA_VERSION,
        "install": install_id,
        "run": _h(record.run_id, secret, prefix="r_"),
        "argus_version": record.argus_version,
        "langgraph_version": _langgraph_version(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "ci": ci,
        "fingerprint": _fingerprint(record, secret),
        "status": _word(record.overall_status),
        "duration_ms": None if record.duration_ms is None else max(int(record.duration_ms), 0),
        "graph": {
            "nodes": len(record.graph_node_names),
            "edges": edges,
            "cyclic": record.is_cyclic,
        },
        "coverage": {
            k: float(v)
            for k, v in record.coverage_summary.items()
            if k in ("structural", "heuristic", "judge")
        },
        "blamed": [_h(n, secret) for n in record.root_cause_chain[:50]],
        "steps": [_step(e, secret) for e in record.steps[:MAX_STEPS]],
        "findings": [_finding(f, secret) for f in record.findings[:MAX_FINDINGS]],
    }
