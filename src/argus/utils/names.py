"""The naming rule for anything shape-only (the run card, ``argus fix --sanitized``).

A node or tool name is chosen in code, so it may be shown as written. A state key or a
field path is not always: a dict can be keyed by the very value a privacy rule protects
(``{"123-45-6789": ...}``). A key is therefore shown only when it *looks like an identifier*
(or a list index); anything else becomes ``?``.

A field path is written with dots, and a key can contain one (``{"john.smith": ...}``), so the
path alone cannot say where one key ends. ``safe_path`` therefore reads each segment against the
data the path points into, and shows a segment only if it is a real key there.

The regular expressions here are mirrored in ``data/cloud_card.schema.json``; a test checks
that every name this module can produce validates against it.
"""

from __future__ import annotations

import re
from typing import Any

from argus.inspector import _as_json_payload

UNKNOWN = "?"
MAX_SEGMENTS = 8

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# One dotted segment: an optional identifier, then any number of list indices ("items[0]", "[0]").
_SEGMENT = re.compile(r"((?:[A-Za-z_][A-Za-z0-9_]{0,63})?)((?:\[[0-9]{1,6}\])*)")
_INDEX = re.compile(r"[0-9]+")


def safe_key(key: Any) -> str:
    """``key`` when it is text that looks like an identifier, else ``?``."""
    return key if isinstance(key, str) and _IDENTIFIER.fullmatch(key) else UNKNOWN


def safe_path(path: Any, *roots: Any) -> str | None:
    """A dotted field path with each segment judged on its own; ``None`` for no path.

    ``roots`` are the values the path points into (``step_roots``: what a step returned, what
    it was given and what its tools answered). A segment is shown only if it looks like an
    identifier or a list index **and** is a real key, or an index in range, at its level in
    one of the roots. A segment that is not, and every segment after it, becomes ``?``:
    ``accounts.john.smith.balance`` over ``{"accounts": {"john.smith": {...}}}`` is
    ``accounts.?.?.?``, not three names. A key that is real but does not look like an identifier
    is shown as ``?`` and the walk goes on under it (``customer.<value>.balance`` is
    ``customer.?.balance``).

    The first segment may be missing from the data (a field nobody wrote); it is shown only
    when it is the whole path, because with more segments after it, it could be the start of a
    dotted key. At most ``MAX_SEGMENTS`` segments are kept.
    """
    if path is None or path == "":
        return None
    parts = (path if isinstance(path, str) else str(path)).split(".")[:MAX_SEGMENTS]
    containers = [root for root in roots if isinstance(root, (dict, list, tuple))]
    best: list[str] | None = None
    best_verified = -1
    cap = len(parts)
    for root in containers or [{}]:
        shown, verified, clash = _read(parts, [root])
        if verified > best_verified:
            best, best_verified = shown, verified
        if clash:
            cap = min(cap, verified)
    result = list(best or [])
    return ".".join(result[:cap] + [UNKNOWN] * (len(result) - cap))


def _read(parts: list[str], nodes: list[Any]) -> tuple[list[str], int, bool]:
    """Walk ``parts`` through ONE root: the text of each, how many were real keys, and whether
    the first one that was not is the start of a dotted key in that root (``john`` beside
    ``john.smith``). Roots are never mixed between segments: a path is read against one
    root at a time."""
    shown: list[str] = []
    verified = 0
    clash = False
    for part in parts:
        before = nodes
        text, nodes = _walk(part, nodes, whole=len(parts) == 1)
        shown.append(text)
        if nodes:
            verified += 1
        elif not clash and part:
            clash = any(
                isinstance(k, str) and k.startswith(part + ".")
                for n in before
                if isinstance(n, dict)
                for k in n
            )
    return shown, verified, clash


def _walk(part: str, nodes: list[Any], *, whole: bool) -> tuple[str, list[Any]]:
    """The text to show for ``part``, and the values under it. No values: nothing is trusted."""
    match = _SEGMENT.fullmatch(part) if part else None
    if match is None:  # not a name: it can be walked through but never shown
        return UNKNOWN, [n[part] for n in nodes if isinstance(n, dict) and part in n]
    name, indices = match.group(1), _INDEX.findall(match.group(2))
    below = nodes
    if name:
        below = [n[name] for n in below if isinstance(n, dict) and name in n]
    for index in map(int, indices):
        below = [n[index] for n in below if isinstance(n, (list, tuple)) and index < len(n)]
    if below:
        return part, below
    return (part, []) if whole and name else (UNKNOWN, [])


def step_roots(step: Any) -> list[Any]:
    """The data the field paths of one step point into.

    What the step returned, the state it was given, and each tool call's output under the
    tool's name: a tool failure's path is ``<tool>.<path in its output>``. Read as the
    inspector reads a tool payload (JSON text parsed, a list as ``{"results": [...]}``).
    """
    roots = [getattr(step, "output_dict", None), getattr(step, "input_state", None)]
    for call in getattr(step, "tool_calls", None) or []:
        if isinstance(call, dict):
            roots.append({str(call.get("name") or "tool"): _tool_payload(call.get("output"))})
    return roots


def _tool_payload(output: Any) -> Any:
    if isinstance(output, str):
        output = _as_json_payload(output)
    return {"results": output} if isinstance(output, list) else output
