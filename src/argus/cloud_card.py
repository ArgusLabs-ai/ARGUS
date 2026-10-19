"""Run card: the only thing a team workspace receives about a run.

Built by allowlist, as ``argus.telemetry.card`` is: every field below is named on purpose and
nothing is copied from the run wholesale, so no code path can carry a value out. Unlike the
telemetry card it keeps names readable, so the rules are about what a name may be:

- Node names, tool names, the agent and the environment are chosen in code. They are sent as
  written, bounded in length.
- A top-level update key and each ``field_path`` segment are sent only when they look like an
  identifier (``argus.utils.names``), otherwise as ``?``: a dict can be keyed by the very value
  being protected. Keys nested inside a value are never sent at all.
- A value becomes ``{t, len, f}`` (``argus.telemetry.card.value_shape``): its type, its length
  and features from a fixed list. A boolean carries no value, and no number is sent, only that
  one was there.
- A finding is sent without its ``reason``, which quotes values. A tool call is its name, an
  HTTP status and a retry class; its input, output and error text stay behind.
- The fix prompt is the ``sanitized`` one.

The published contract is ``argus/data/cloud_card.schema.json``; it lists every field and
sets ``additionalProperties: false``. See docs/DATA.md.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from argus.failure_class import (
    classify_failure_class,
    coerce_status,
    status_from_text,
    valid_failure_class,
)
from argus.findings import _fid
from argus.fix_prompt import FixPromptError, build_fix_prompt_for_record
from argus.inspector import _STATUS_KEYS, _as_json_payload
from argus.models import Finding, NodeEvent, RunRecord
from argus.telemetry.card import _word, value_shape
from argus.utils.names import safe_key, safe_path, step_roots

logger = logging.getLogger("argus")

SCHEMA_VERSION = 1

# Every bound below is also in the schema; a test keeps the two equal.
MAX_NAME = 128
MAX_SUPERSTEP = 256
MAX_STEPS = 1000
MAX_FINDINGS = 500
MAX_TOOLS = 50
MAX_KEYS = 50
MAX_NODES = 500
MAX_EDGES = 100
MAX_CHAIN = 50
MAX_MODELS = 50
MAX_PROMPT = 50_000

_ANOMALY_ID = re.compile(r"[A-Z]{2}-[0-9]{3}")  # BA-001: a rule id, not free text
_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,32}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,63}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.]{1,20}(?:Z|[+-][0-9:]{1,6})?")


def _text(value: Any, limit: int = MAX_NAME) -> str:
    """A name chosen in code, as written, bounded and never empty."""
    return str(value)[:limit] or "?"


def _only(pattern: re.Pattern[str], value: Any, otherwise: str) -> str:
    text = str(value or "").strip()
    return text if pattern.fullmatch(text) else otherwise


def _type(value: Any) -> str:
    """A finding type: a rule id (``BA-001``) or a short snake_case word, else ``other``.

    A learned signature's category and an LLM review kind (``review_<kind>``) are free text.
    """
    text = str(value or "").strip()
    return text if _ANOMALY_ID.fullmatch(text) else _word(text)


def _number(value: Any) -> float | None:
    """``value`` as a finite float, else None (a card must be valid JSON)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _ms(value: Any) -> float | None:
    number = _number(value)
    return None if number is None or number < 0 else round(number, 2)


def _count(value: Any) -> int:
    number = _number(value)
    return 0 if number is None or number < 0 else int(number)


def _run(record: RunRecord) -> dict[str, Any]:
    started = str(record.started_at or "").strip()
    return {
        "run_id": _text(record.run_id),
        "argus_version": _only(_VERSION, record.argus_version, "unknown"),
        "overall_status": _word(record.overall_status),
        "complete": bool(record.complete),
        "end_reason": _word(record.end_reason) if record.end_reason else None,
        "duration_ms": _ms(record.duration_ms),
        "started_at": started if _STAMP.fullmatch(started) else None,
        "agent": _text(record.agent) if record.agent else None,
        "environment": _text(record.environment) if record.environment else None,
        "test_id": None,  # RunRecord does not carry it; a pytest node id can hold parameter values
    }


def _topology(record: RunRecord) -> dict[str, Any]:
    edges = record.graph_edge_map if isinstance(record.graph_edge_map, dict) else {}
    return {
        "graph_node_names": [_text(n) for n in (record.graph_node_names or [])[:MAX_NODES]],
        "graph_edge_map": {
            _text(source): [_text(dest) for dest in list(dests or [])[:MAX_EDGES]]
            for source, dests in list(edges.items())[:MAX_NODES]
        },
    }


def _update(update: Any) -> dict[str, Any] | None:
    """The shape of a node's update: each top-level key (named by the naming rule) and the
    shape of its value. Several keys that are not identifiers share one ``?`` entry; ``len``
    still counts them all."""
    if not isinstance(update, dict):
        return None
    keys: dict[str, Any] = {}
    for key, value in list(update.items())[:MAX_KEYS]:
        keys.setdefault(safe_key(key), value_shape(value, name=None, bools=False))
    return {"t": "object", "len": len(update), "keys": keys}


def _status(call: dict[str, Any]) -> int | None:
    """The HTTP status a tool call reports: in its output, else in its error text."""
    payload = call.get("output")
    if isinstance(payload, str):
        payload = _as_json_payload(payload)
    if isinstance(payload, dict):
        for key in sorted(_STATUS_KEYS):  # a set: sort, so one payload always reads the same
            code = coerce_status(payload.get(key))
            if code is not None:
                return code
    return status_from_text(call.get("error"))


def _tool(call: dict[str, Any]) -> dict[str, Any]:
    error = call.get("error")
    status = _status(call)
    failed = bool(error) or (status is not None and status >= 400)
    failure_class = (
        classify_failure_class(
            "tool_error" if error else "error_response", status, str(error) if error else None
        )
        if failed
        else None
    )
    return {
        "name": _text(call.get("name") or "tool"),
        "status": status,
        "failure_class": failure_class,
        "error": bool(error),
    }


def _step(event: NodeEvent) -> dict[str, Any]:
    return {
        "node": _text(event.node_name),
        "status": _word(event.status),
        "superstep": _text(event.superstep, MAX_SUPERSTEP) if event.superstep else None,
        "duration_ms": _ms(event.duration_ms) or 0.0,
        "update": _update(event.output_dict),
        "tools": [_tool(c) for c in (event.tool_calls or [])[:MAX_TOOLS] if isinstance(c, dict)],
    }


def _roots_by_node(record: RunRecord) -> dict[str, list[Any]]:
    """The data each node's field paths point into: every step of the node, as ``step_roots``."""
    roots: dict[str, list[Any]] = {}
    for event in record.steps or []:
        roots.setdefault(event.node_name, []).extend(step_roots(event))
    return roots


def _finding(finding: Finding, roots: list[Any]) -> dict[str, Any]:
    """No ``reason``. The id is recomputed from what is sent: the local one hashes the raw
    path, so anyone could test a guess at a masked segment against it. Where nothing was
    masked the two are equal. ``roots`` is the data of the finding's node, which the path is
    read against."""
    kind = _type(finding.type)
    node = _text(finding.node)
    source = _word(finding.source)
    path = safe_path(finding.field_path, *roots)
    confidence = _number(finding.confidence)
    return {
        "id": _fid(kind, node, path, source),
        "type": kind,
        "node": node,
        "field_path": path,
        "origin_node": _text(finding.origin_node) if finding.origin_node else None,
        "severity": _word(finding.severity),
        "source": source,
        "confidence": confidence,
        "failure_class": valid_failure_class(finding.failure_class),
        "suppressed": bool(finding.suppressed),
    }


def _llm_usage(record: RunRecord) -> list[dict[str, Any]]:
    """Calls and token counts per model. No cost, finish reason or model output."""
    totals: dict[str, list[int]] = {}
    for event in record.steps or []:
        for call in event.llm_usage.calls if event.llm_usage else []:
            row = totals.setdefault(_only(_MODEL, call.model_name, "other"), [0, 0, 0, 0])
            row[0] += 1
            row[1] += _count(call.prompt_tokens)
            row[2] += _count(call.completion_tokens)
            row[3] += _count(call.total_tokens)
    return [
        {
            "model": model,
            "calls": calls,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
        for model, (calls, prompt, completion, total) in sorted(totals.items())[:MAX_MODELS]
    ]


def _fix_prompt(record: RunRecord) -> str | None:
    """The sanitized fix prompt of a run that failed; None when there is nothing to fix.

    The prompt is the one part built by a large renderer that reads the whole record, so a
    record it cannot render (a hand-edited run file) costs the card its prompt, not the card.
    The log names the exception class only: its message can quote a value.
    """
    if record.overall_status == "clean":
        return None
    try:
        return build_fix_prompt_for_record(record, sanitized=True).prompt[:MAX_PROMPT]
    except FixPromptError:
        return None
    except Exception as exc:
        logger.warning("argus: no fix prompt in the run card (%s)", type(exc).__name__)
        return None


def build_card(record: RunRecord) -> dict[str, Any]:
    """The run card for ``record``. Pure but for the fix prompt, which reads source files to
    name the function to edit (``argus fix`` does the same)."""
    roots = _roots_by_node(record)
    return {
        "schema": SCHEMA_VERSION,
        "run": _run(record),
        "topology": _topology(record),
        "steps": [_step(e) for e in (record.steps or [])[:MAX_STEPS]],
        "findings": [
            _finding(f, roots.get(f.node, [])) for f in (record.findings or [])[:MAX_FINDINGS]
        ],
        "llm_usage": _llm_usage(record),
        "root_cause_chain": [_text(n) for n in (record.root_cause_chain or [])[:MAX_CHAIN]],
        "fix_prompt": _fix_prompt(record),
    }
