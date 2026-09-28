"""Whole-trace rules: silent failures only the finished fat trace can show.

The inspector grades one step as it ends. These rules read the whole run once
it is over, because their evidence spans steps (a claimed refund with no refund
call anywhere in the run, a loop repeating one call) or needs something a
single step does not carry (the graph's state schema, a healthy baseline, the
model's raw output). Each hit is critical and lands on the step that caused it.

Measured on a 255-fault taxonomy suite (23 classes, 6 pipelines, 20 vendor
response shapes): coverage 51% → ~92%, with no false positive on 63 healthy
runs, 54 of them real model prose.

    D1  unknown_state_key       a typo'd key: not in the state, near a declared key never written
    D2  error_response          vendor error body the inspector's vocabulary missed
    D3  empty_result            an empty lookup, whatever its keys are called
    D4  unfollowed_pagination   a first page carried forward as the whole result
    D5  type_drift              output type differs from the healthy baseline
    D6  sentinel_value          N/A / unknown / -1 where the healthy baseline has data
    D8  unrendered_template     {{var}}, lorem ipsum, "Dear [Name]" written by a model node
    D9  degenerate_repetition   a model node's output repeating itself
    D10 truncated_output        a generation cut at its token limit, and used
    D11 unparseable_model_json  the model's JSON did not parse; the node carried on
    D12 ungrounded_number       a number in a model node's output nothing it was given supports
    D13 near_miss_identifier    an ID one or two edits from the one the node was given
    D14 unperformed_action      "I've refunded …" with no such tool call anywhere in the run
    D15 stuck_loop              the same tool call, same arguments, three times
    D16 missing_output_key      a key the healthy baseline says this node always writes

D5 / D6 / D16 need a baseline (``argus baseline``); without one they are off.
Text rules (D8–D14) only read nodes the trace shows making a model call.

**Consequences are not re-blamed.** A hit on a step that runs after another
node already failed is dropped: a lookup that comes back empty because the step
before it passed a bad ID is that step's failure, not a second one. The judge
follows the same rule (``_demote_consequential_judge_fails``).

Imports nothing from ``langgraph`` / ``langchain_core``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["Hit", "build_baseline", "run_rules"]

# D1 and D16 hits mean "this node did not produce the field under the right
# name". The contextual layer's "never written → first step" guess defers to them.
PRODUCED_NOTHING = frozenset({"unknown_state_key", "missing_output_key"})


@dataclass(frozen=True)
class Hit:
    event: Any
    failure_type: str
    field_name: str
    evidence: str


# ── shared helpers ───────────────────────────────────────────────────────────


def _leaves(v: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(v, dict):
        for k, x in v.items():
            yield from _leaves(x, f"{path}.{k}" if path else str(k))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _leaves(x, f"{path}[{i}]")
    else:
        yield path, v


def _dump(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, default=str)


def _update(e: Any) -> dict[str, Any]:
    return e.output_dict if isinstance(e.output_dict, dict) else {}


def _tool_outputs(e: Any) -> list[Any]:
    return [t.get("output") for t in (e.tool_calls or [])]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _model_calls(e: Any) -> list[Any]:
    usage = getattr(e, "llm_usage", None)
    return list(usage.calls) if usage and getattr(usage, "calls", None) else []


def _authored_text(e: Any) -> Iterator[tuple[str, str]]:
    """String leaves the node wrote itself — not text it was handed and passed on."""
    inherited = _dump(e.input_state or {})
    for path, v in _leaves(_update(e)):
        if isinstance(v, str) and v and v not in inherited:
            yield path, v


def _is_origin(e: Any) -> bool:
    if getattr(e, "status", None) in ("fail", "crashed", "semantic_fail"):
        return True
    insp = getattr(e, "inspection", None)
    return bool(insp is not None and insp.is_silent_failure and insp.severity == "critical")


# ── D1 ───────────────────────────────────────────────────────────────────────


def _d1(
    e: Any,
    state_keys: list[str],
    node_state_keys: dict[str, list[str]],
    written: set[str],
    declared: set[str],
    baseline: dict[str, Any] | None,
    **_: Any,
) -> Iterator[tuple[str, str, str]]:
    """A key the state lacks, 1–2 edits from a *declared* key nothing in the run wrote.

    Extra keys are common and harmless (a parsed LLM reply's `reasoning`, a
    `result_a` beside `result_b`); LangGraph drops them and nothing was lost. A
    typo of a key the team declared it depends on — in ``consumers=`` or in the
    node's baseline — is not: the key it meant never arrives.
    """
    keys = set(node_state_keys.get(e.node_name) or state_keys or ())
    if not keys:
        return
    base_keys = set(((baseline or {}).get("nodes", {}).get(e.node_name) or {}).get("keys", []))
    wanted = (keys & (declared | base_keys)) - written
    for k in sorted(_update(e)):
        if str(k).startswith("__") or k in keys or len(str(k)) < 4:
            continue
        meant = sorted(r for r in wanted if _edits(str(k).lower(), r.lower()) <= 2)
        if meant:
            yield (
                "unknown_state_key",
                k,
                (
                    f"wrote `{k}`, which the graph state does not have — LangGraph drops it, "
                    f"and `{meant[0]}` is never written"
                ),
            )
            return


# ── D2 / D3 / D4: tool responses ─────────────────────────────────────────────

_ERR_KEYS = {
    "error",
    "errors",
    "errorcode",
    "error_code",
    "errormessages",
    "fault",
    "faultstring",
    "exception",
}
_ERR_TEXT = re.compile(
    r"^\s*(error\b|exception\b|traceback|<html|<!doctype html"
    r"|\d{3} (bad gateway|service unavailable|internal server error|gateway timeout))",
    re.I,
)


def _is_error_payload(o: Any, depth: int = 0) -> bool:
    if depth > 3:
        return False
    if isinstance(o, str):
        return bool(_ERR_TEXT.match(o))
    if isinstance(o, list):
        return bool(o) and all(isinstance(x, dict) and _is_error_payload(x, depth + 1) for x in o)
    if not isinstance(o, dict):
        return False
    low = {str(k).lower(): v for k, v in o.items()}
    if any(k in _ERR_KEYS and v not in (None, "", [], {}, False) for k, v in low.items()):
        return True
    if str(low.get("__type", "")).endswith("Exception"):
        return True
    if low.get("object") == "error" or low.get("ok") is False or low.get("success") is False:
        return True
    if str(low.get("status", "")).lower() in {"error", "fail", "failed", "failure"}:
        return True
    code = low.get("status") if _is_num(low.get("status")) else low.get("code")
    if (
        "message" in low
        and isinstance(code, (int, float))
        and not isinstance(code, bool)
        and code >= 400
    ):
        return True
    return "message" in low and "documentation_url" in low


_COUNT_KEYS = {"total", "totalsize", "count", "total_count", "totalrows", "numrows", "resultcount"}
_META_KEYS = {
    "kind",
    "object",
    "url",
    "namespace",
    "took",
    "done",
    "has_more",
    "next_page",
    "@odata.context",
    "majordimension",
    "range",
    "jobcomplete",
    "startat",
    "maxresults",
    "incomplete_results",
    "status",
    "success",
    "ok",
    "code",
    "message",
} | _COUNT_KEYS
_NO_RESULTS = re.compile(r"^\s*no (results|records|matches|data)( (were )?found)?\.?\s*$", re.I)


def _flat(o: dict[str, Any], depth: int = 0) -> Iterator[tuple[str, Any]]:
    for k, v in o.items():
        yield str(k), v
        if isinstance(v, dict) and depth < 3:
            yield from _flat(v, depth + 1)


def _lists(o: Any, depth: int = 0) -> Iterator[list[Any]]:
    if isinstance(o, dict) and depth < 4:
        for v in o.values():
            if isinstance(v, list):
                yield v
            elif isinstance(v, dict):
                yield from _lists(v, depth + 1)


def _is_empty_lookup(o: Any) -> bool:
    """An empty *lookup*. ``None`` / ``""`` are left alone: side-effect tools return them."""
    if isinstance(o, str):
        return bool(_NO_RESULTS.match(o))
    if isinstance(o, list):
        return len(o) == 0
    if not isinstance(o, dict):
        return False
    if any(k.lower() in _COUNT_KEYS and str(v) in ("0", "0.0") for k, v in _flat(o)):
        return True
    lists = list(_lists(o))
    has_data = any(
        k.lower() not in _META_KEYS
        and not isinstance(v, (dict, list))
        and v not in (None, "", False)
        for k, v in _flat(o)
    )
    return bool(lists) and all(len(x) == 0 for x in lists) and not has_data


_MORE = {"has_more": True, "hasmore": True, "done": False, "incomplete_results": True}
_NEXT = {
    "next_page_token",
    "nextpagetoken",
    "next_page",
    "nextlink",
    "@odata.nextlink",
    "next_cursor",
}


def _already_graded(e: Any, tool: str) -> bool:
    """The inspector already failed this tool call — do not report it twice."""
    insp = getattr(e, "inspection", None)
    if insp is None:
        return False
    return any(
        tf.severity == "critical"
        and (tf.field_name == tool or tf.field_name.startswith(f"{tool}."))
        for tf in insp.tool_failures
    )


def _tools(e: Any, allow_empty_fields: frozenset[str], **_: Any) -> Iterator[tuple[str, str, str]]:
    calls = e.tool_calls or []
    softened = bool(allow_empty_fields & set(_update(e)))
    for i, t in enumerate(calls):
        name, out = str(t.get("name") or "tool"), t.get("output")
        if t.get("error") or _already_graded(e, name):
            continue
        if _is_error_payload(out):
            yield (
                "error_response",
                name,
                f"{name} returned an error payload the node stored as data",
            )
        elif not softened and _is_empty_lookup(out):
            yield "empty_result", name, f"{name} came back empty and the node carried on"
        elif isinstance(out, dict):
            flat = {k.lower(): v for k, v in _flat(out)}
            more = any(flat.get(k) == v for k, v in _MORE.items()) or any(
                flat.get(k) for k in _NEXT
            )
            followed = any(c.get("name") == t.get("name") for c in calls[i + 1 :])
            page = next((len(x) for x in _lists(out) if x), 0)
            carried = any(isinstance(v, list) and len(v) == page for v in _update(e).values())
            if more and page and carried and not followed:
                yield (
                    "unfollowed_pagination",
                    name,
                    (
                        f"{name} said more pages exist; the node passed on page one "
                        "as the whole result"
                    ),
                )


# ── D5 / D6 / D16: healthy baseline ──────────────────────────────────────────

_SENTINELS = {"n/a", "na", "unknown", "null", "tbd", "-", "undefined", "nan"}


def _type_name(v: Any) -> str:
    return "number" if _is_num(v) else type(v).__name__


def _tag(v: Any) -> str | None:
    """What a healthy value held — never the value itself (a baseline file must not carry data)."""
    if isinstance(v, str) and v.strip() and v.strip().lower() not in _SENTINELS:
        return "text"
    if _is_num(v) and v >= 0:
        return "nonneg"
    if isinstance(v, dict) and v:
        return "object"
    return None


def _all_paths(v: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every path, containers included: a list item that was a record can come back as "N/A"."""
    if path:
        yield path, v
    if isinstance(v, dict):
        for k, x in v.items():
            yield from _all_paths(x, f"{path}.{k}" if path else str(k))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _all_paths(x, f"{path}[{i}]")


def _baseline_rules(
    e: Any, baseline: dict[str, Any] | None, **_: Any
) -> Iterator[tuple[str, str, str]]:
    node = (baseline or {}).get("nodes", {}).get(e.node_name)
    if not node:
        return
    upd = _update(e)
    lost = [k for k in node.get("keys", []) if k not in upd]
    if lost:
        yield (
            "missing_output_key",
            lost[0],
            f"did not write {lost}, which it writes on every healthy run",
        )
        return
    for k, want in node.get("types", {}).items():
        if k in upd and upd[k] is not None and _type_name(upd[k]) != want:
            yield "type_drift", k, f"`{k}` is {_type_name(upd[k])}; healthy runs write {want}"
            return
    paths = node.get("paths", {})
    for path, v in _leaves(upd):
        tag = paths.get(path) or paths.get(re.sub(r"\[\d+\]", "[0]", path))
        sentinel = (
            tag in ("text", "object") and isinstance(v, str) and v.strip().lower() in _SENTINELS
        ) or (tag == "nonneg" and _is_num(v) and v < 0)
        if sentinel:
            yield "sentinel_value", path, f"`{path}` = {v!r}; healthy runs hold real data here"
            return


def build_baseline(runs: Iterable[Iterable[Any]]) -> dict[str, Any]:
    """What every healthy run agrees on, per node: keys written, their types, leaf kinds.

    Pass several runs — one per branch, ideally a few per node. Only what is true
    of *every* visit survives, so a key written on one path is not required on
    another. Holds kinds (``text``, ``nonneg``), never values.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for events in runs:
        for e in events:
            if getattr(e, "status", "pass") in ("skipped", "retried", "fail", "crashed"):
                continue
            upd = _update(e)
            keys = set(upd)
            types = {k: _type_name(v) for k, v in upd.items() if v is not None}
            paths = {re.sub(r"\[\d+\]", "[0]", p): t for p, v in _all_paths(upd) if (t := _tag(v))}
            cur = nodes.get(e.node_name)
            if cur is None:
                nodes[e.node_name] = {"keys": keys, "types": types, "paths": paths}
                continue
            cur["keys"] &= keys
            cur["types"] = {k: t for k, t in cur["types"].items() if types.get(k) == t}
            cur["paths"] = {p: t for p, t in cur["paths"].items() if paths.get(p) == t}
    return {
        "version": 1,
        "nodes": {
            n: {
                "keys": sorted(v["keys"]),
                "types": dict(sorted(v["types"].items())),
                "paths": v["paths"],
            }
            for n, v in sorted(nodes.items())
        },
    }


# ── D8–D11: model output ─────────────────────────────────────────────────────

_TEMPLATE = re.compile(
    r"\{\{\s*[A-Za-z_][\w.]*\s*\}\}|\blorem ipsum\b|^\s*<\w+>\s*$|^\s*(TODO|TBD|FIXME)\s*$"
    r"|\b(Dear|Hi|Hello)\s+\[[A-Z][^\]]{1,30}\]",
    re.I | re.M,
)


def _repetition(text: str, n: int = 3) -> float:
    w = text.lower().split()
    if len(w) < 12:
        return 0.0
    grams = [tuple(w[i : i + n]) for i in range(len(w) - n + 1)]
    return 1 - len(set(grams)) / len(grams)


def _model_output(e: Any, **_: Any) -> Iterator[tuple[str, str, str]]:
    calls = _model_calls(e)
    if not calls:
        return
    authored = list(_authored_text(e))
    for path, v in authored:
        low = path.lower()
        if "template" not in low and "prompt" not in low and _TEMPLATE.search(v):
            yield "unrendered_template", path, f"`{path}` still holds unrendered template text"
            return
        if _repetition(v) > 0.5:
            yield "degenerate_repetition", path, f"`{path}` is the model repeating itself"
            return
    if any(c.finish_reason in ("length", "max_tokens") for c in calls):
        long = next(((p, v) for p, v in authored if len(v) > 40), None)
        if long:
            yield (
                "truncated_output",
                long[0],
                "the model hit its token limit and the cut-off text was used",
            )
            return
    for c in calls:
        text = (getattr(c, "output_text", None) or "").strip()
        if "{" not in text and not text.startswith("["):
            continue
        body = (
            re.sub(r"^.*?```(?:json)?\s*|\s*```\s*$", "", text, flags=re.S)
            if "```" in text
            else text
        )
        try:
            json.loads(body)
        except ValueError:
            if body.lstrip()[:1] in "{[":
                yield (
                    "unparseable_model_json",
                    "_output",
                    "the model's JSON did not parse and the node used a fallback",
                )
                return


# ── D12 / D13: grounding ─────────────────────────────────────────────────────

_NUM = re.compile(
    r"(?<![\w.])\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?[KMB]?|(?<![\w.])\$?\d+(?:\.\d+)?%?[KMB]?(?![\w-])"
)
_WORD_MULT = re.compile(r"\s*(thousand|million|billion|bn|mn)\b", re.I)
_WORD_VAL = {"thousand": 1e3, "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9}


def _numbers(text: str) -> set[float]:
    out: set[float] = set()
    for m in _NUM.finditer(text):
        tok = m.group(0)
        raw = tok.replace("$", "").replace(",", "").rstrip("%")
        mult = 1.0
        if raw and raw[-1] in "KMB":
            mult, raw = {"K": 1e3, "M": 1e6, "B": 1e9}[raw[-1]], raw[:-1]
        try:
            v = float(raw) * mult
        except ValueError:
            continue
        w = _WORD_MULT.match(text, m.end())
        if w and mult == 1.0:
            v *= _WORD_VAL[w.group(1).lower()]
        elif v < 100 and not ("$" in tok or "%" in tok or "." in tok):
            continue  # small integers: counts, days, list positions
        out.add(round(v, 2))
    return out


def _list_sums(v: Any, acc: set[float]) -> None:
    if isinstance(v, dict):
        for x in v.values():
            _list_sums(x, acc)
    elif isinstance(v, list):
        nums = [x for x in v if _is_num(x)]
        if nums:
            acc.add(round(float(sum(nums)), 2))
        by_key: dict[str, float] = {}
        for x in v:
            if isinstance(x, dict):
                for k, y in x.items():
                    if _is_num(y):
                        by_key[k] = by_key.get(k, 0.0) + float(y)
            _list_sums(x, acc)
        acc.update(round(t, 2) for t in by_key.values())


def _evidence_numbers(e: Any) -> set[float]:
    given = [e.input_state or {}, _tool_outputs(e)]
    nums = _numbers(_dump(given))
    for _, v in _leaves(given):
        if _is_num(v):
            nums.add(round(float(v), 2))
        elif isinstance(v, str):
            try:
                nums.add(round(float(v), 2))
            except ValueError:
                pass
    _list_sums(given, nums)
    # the arithmetic a step may legitimately do; bounded so a large payload cannot explode it
    base = sorted(nums, key=abs, reverse=True)[:40]
    for i, a in enumerate(base):
        for b in base[i + 1 :]:
            nums.update({round(a + b, 2), round(a - b, 2), round(b - a, 2)})
            if b:
                nums.add(round((a / b - 1) * 100, 1))
            if a:
                nums.add(round((b / a - 1) * 100, 1))
    return nums


def _d12(e: Any, **_: Any) -> Iterator[tuple[str, str, str]]:
    if not _model_calls(e):
        return
    have = None
    for path, v in _leaves(_update(e)):
        if isinstance(v, str):
            vals = _numbers(v)
        elif _is_num(v) and abs(v) >= 100:
            vals = {round(float(v), 2)}
        else:
            continue
        if not vals:
            continue
        have = have if have is not None else _evidence_numbers(e)
        bad = [x for x in vals if not any(abs(x - h) <= max(0.005 * abs(h), 0.01) for h in have)]
        if bad:
            yield (
                "ungrounded_number",
                path,
                f"`{path}` states {bad[0]:g}, which nothing the node was given supports",
            )
            return


_ID = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b(?:[a-z0-9-]+\.)+(?:com|co|io|net|org|ai|dev|gg)\b"
    r"|\b[A-Z]{1,5}-\d{2,}\b"
    r"|\b\d{4}-\d{2}(?:-\d{2})?\b"
)


def _present(tok: str, text: str) -> bool:
    return re.search(r"(?<![\w.-])" + re.escape(tok) + r"(?![\w-]|\.\w)", text) is not None


def _edits(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _near_miss(tok: str, evidence: str) -> str | None:
    if _present(tok, evidence):
        return None
    for m in _ID.finditer(evidence):
        real = m.group(0)
        if real != tok and _edits(tok.lower(), real.lower()) <= 2:
            return real
    return None


def _d13(e: Any, **_: Any) -> Iterator[tuple[str, str, str]]:
    given = _dump(e.input_state or {})
    calls = e.tool_calls or []
    for i, t in enumerate(calls):
        seen = given + " " + _dump([c.get("output") for c in calls[:i]])
        for m in _ID.finditer(str(t.get("input", ""))):
            real = _near_miss(m.group(0), seen)
            if real:
                yield (
                    "near_miss_identifier",
                    str(t.get("name")),
                    (f"{t.get('name')} called with {m.group(0)!r}; the node was given {real!r}"),
                )
                return
    if _model_calls(e):
        seen = given + " " + _dump(_tool_outputs(e))
        for path, v in _leaves(_update(e)):
            for m in _ID.finditer(str(v)):
                real = _near_miss(m.group(0), seen)
                if real:
                    yield (
                        "near_miss_identifier",
                        path,
                        f"`{path}` names {m.group(0)!r}; the node was given {real!r}",
                    )
                    return


# ── D14 / D15: the run as a whole ────────────────────────────────────────────

_CLAIM = re.compile(
    r"\b(?:I(?:'ve| have)?|we(?:'ve| have)?|has been|have been|was|were|successfully)\s+"
    r"(refund(?:ed)?|cancel(?:l?ed)?|reset|delet(?:ed)?|sent|book(?:ed)?|charg(?:ed)?|"
    r"issu(?:ed)?|approv(?:ed)?|unlock(?:ed)?|schedul(?:ed)?|transferr?(?:ed)?|paid)\b",
    re.I,
)
_NEG = re.compile(r"\b(no|not|never|cannot|couldn't|could not|unable|nothing|none)\b|n't\b", re.I)
_STEM = {"sent": "send", "paid": "pay", "issu": "refund"}


def _performed(events: list[Any]) -> set[str]:
    return {
        str(t.get("name", "")).lower()
        for e in events
        for t in (e.tool_calls or [])
        if not t.get("error") and not _is_error_payload(t.get("output"))
    }


def _d14(e: Any, performed: set[str], **_: Any) -> Iterator[tuple[str, str, str]]:
    if not _model_calls(e):
        return
    for path, v in _authored_text(e):
        for m in _CLAIM.finditer(v):
            if _NEG.search(v[max(0, m.start() - 30) : m.start()]):
                continue
            verb = m.group(1).lower()
            stem = next(
                (s for k, s in _STEM.items() if verb.startswith(k)),
                re.sub(r"(ed|d)$", "", verb)[:5],
            )
            if not any(stem in name for name in performed):
                yield (
                    "unperformed_action",
                    path,
                    (
                        f"`{path}` says “{m.group(0)}”, but no successful tool call "
                        "in the run did that"
                    ),
                )
                return


def _d15(events: list[Any]) -> list[Hit]:
    counts: dict[tuple[str, str], int] = {}
    for e in events:
        for t in e.tool_calls or []:
            key = (str(t.get("name")), str(t.get("input")))
            counts[key] = counts.get(key, 0) + 1
    stuck = next((k for k, n in counts.items() if n >= 3), None)
    if stuck is None:
        return []
    name = stuck[0]
    deciders = [e for e in events if not e.tool_calls and name in _dump(_update(e))]
    target = (
        deciders[-1]
        if deciders
        else [e for e in events if any(t.get("name") == name for t in e.tool_calls or [])][-1]
    )
    return [
        Hit(
            target,
            "stuck_loop",
            name,
            f"{name} was called with the same arguments 3+ times; the loop made no progress",
        )
    ]


# ── entry point ──────────────────────────────────────────────────────────────

_STEP_RULES = (_d1, _tools, _baseline_rules, _model_output, _d12, _d13, _d14)


def run_rules(
    events: list[Any],
    *,
    state_keys: Iterable[str] = (),
    node_state_keys: Mapping[str, Iterable[str]] | None = None,
    consumers: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> list[Hit]:
    """Every rule over one finished run. Steps after an earlier origin are skipped."""
    ran = [e for e in events if getattr(e, "status", "pass") != "skipped"]
    allow_empty = frozenset(
        k
        for k, spec in (consumers or {}).items()
        if isinstance(spec, dict) and spec.get("allow_empty")
    )
    ctx: dict[str, Any] = {
        "state_keys": list(state_keys or ()),
        "node_state_keys": {k: list(v) for k, v in (node_state_keys or {}).items()},
        "allow_empty_fields": allow_empty,
        "baseline": baseline,
        "performed": _performed(ran),
        "written": {k for e in ran for k in _update(e)},
        "declared": {str(k).split(".")[0] for k in (consumers or {})},
    }
    found_by: list[tuple[Any, Hit]] = []  # (rule, hit)
    failed: set[str] = set()  # nodes that are already origins, in step order
    for e in ran:
        if failed - {e.node_name}:
            break  # everything from here on is downstream of an origin
        for rule in _STEP_RULES:
            found = next(iter(rule(e, **ctx)), None)
            if found:
                found_by.append((rule, Hit(e, *found)))
                break
        if _is_origin(e) or (found_by and found_by[-1][1].event is e):
            failed.add(e.node_name)
    hits = [_last_visit(rule, hit, ran, ctx) for rule, hit in found_by]
    if not hits and not any(_is_origin(e) for e in ran):
        hits += _d15(ran)
    return hits


def _last_visit(rule: Any, hit: Hit, ran: list[Any], ctx: dict[str, Any]) -> Hit:
    """In a loop only the last visit counts: earlier ones are relabelled
    ``retried`` at finalize and their findings dropped. If the node's last visit
    shows the same fault, move the hit there; if it does not, the loop
    self-corrected and the hit stays where finalize will retire it."""
    last = [e for e in ran if e.node_name == hit.event.node_name][-1]
    if last is hit.event:
        return hit
    again = next(iter(rule(last, **ctx)), None)
    return Hit(last, *again) if again else hit
