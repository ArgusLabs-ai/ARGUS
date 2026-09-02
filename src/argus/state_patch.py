"""State patching for time-travel replay.

Replay re-runs a pipeline from a saved node using that node's recorded input
state *verbatim*.  This module adds the missing half: editing that state
before it is replayed, so a developer can fix a bad value at step 7 and
resume the remaining steps — instead of fixing code and re-running all ten.

A patch is a plain JSON document with up to three op groups::

    {
      "delete": ["broken_field", "meta.stale_token"],
      "set":    {"query": "fixed query", "meta.retries": 0},
      "merge":  {"config": {"temperature": 0}}
    }

Ops are applied in a fixed, documented order — **delete, then set, then
merge** — so the result never depends on JSON key ordering.  A path that
appears in more than one op group is rejected rather than silently resolved.

Paths are dotted, with list indices in brackets.  Both ``items[0].name`` and
``items.[0].name`` parse identically, so a ``field_path`` reported by a
semantic signal can be pasted straight into a patch.

Nothing here performs I/O or mutates its input: ``apply_patch`` deep-copies
the state and returns a new dict.
"""

from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass
from typing import Any, Union

__all__ = [
    "PatchChange",
    "PatchError",
    "apply_patch",
    "format_changes",
    "parse_path",
    "preview_patch",
    "validate_patch",
]

# Applied in this order regardless of key order in the patch document.
_APPLY_ORDER = ("delete", "set", "merge")
_VALID_OPS = frozenset(_APPLY_ORDER)

_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
_MAX_KEYS_IN_ERROR = 10

# A parsed path segment: a dict key (str) or a list index (int).
Segment = Union[str, int]


class PatchError(ValueError):
    """Raised when a patch is malformed or cannot be applied to a state."""


@dataclass(frozen=True)
class PatchChange:
    """A single value change produced by applying a patch."""

    op: str  # "delete" | "set" | "merge"
    path: str  # the path as written in the patch document
    before: Any  # value before the change (None when it did not exist)
    after: Any  # value after the change (None for deletes)
    existed: bool  # whether the path resolved to an existing value


# ── Path parsing ──────────────────────────────────────────────────────────────


def parse_path(path: str) -> list[Segment]:
    """Parse a dotted path into segments.

    ``"items[0].name"`` and ``"items.[0].name"`` both yield
    ``["items", 0, "name"]``.

    Raises:
        PatchError: if the path is empty or malformed.
    """
    if not isinstance(path, str):
        raise PatchError(f"patch path must be a string, got {type(path).__name__}")
    if not path.strip():
        raise PatchError("patch path must be a non-empty string")

    segments: list[Segment] = []
    for raw in path.split("."):
        if raw == "":
            raise PatchError(
                f"invalid path {path!r}: empty segment "
                "(check for '..' or a leading/trailing '.')"
            )
        segments.extend(_parse_segment(raw, path))

    if not segments:
        raise PatchError(f"invalid path {path!r}: no segments found")
    return segments


def _parse_segment(raw: str, path: str) -> list[Segment]:
    """Parse one dot-delimited chunk, e.g. ``items[0][1]`` or ``[3]`` or ``name``."""
    bracket_at = raw.find("[")
    if bracket_at == -1:
        if "]" in raw:
            raise PatchError(f"invalid path {path!r}: unmatched ']' in segment {raw!r}")
        return [raw]

    out: list[Segment] = []
    name = raw[:bracket_at]
    if name:
        out.append(name)

    pos = bracket_at
    while pos < len(raw):
        match = _BRACKET_RE.match(raw, pos)
        if match is None:
            raise PatchError(
                f"invalid path {path!r}: malformed list index in segment {raw!r} "
                "(expected '[<int>]')"
            )
        inner = match.group(1).strip()
        try:
            out.append(int(inner))
        except ValueError:
            raise PatchError(
                f"invalid path {path!r}: list index '[{match.group(1)}]' is not an integer"
            ) from None
        pos = match.end()

    return out


def _render(segments: list[Segment]) -> str:
    """Render parsed segments back to a readable path, for error messages."""
    parts: list[str] = []
    for seg in segments:
        if isinstance(seg, int):
            parts.append(f"[{seg}]")
        else:
            parts.append(f".{seg}" if parts else seg)
    return "".join(parts)


# ── Traversal ─────────────────────────────────────────────────────────────────


def _key_hint(node: dict[str, Any], key: str) -> str:
    """Build a 'did you mean' / 'available keys' hint for a missing dict key."""
    keys = [str(k) for k in node]
    if not keys:
        return " — the object at that path is empty"
    close = difflib.get_close_matches(key, keys, n=1, cutoff=0.6)
    if close:
        return f" — did you mean {close[0]!r}?"
    shown = sorted(keys)[:_MAX_KEYS_IN_ERROR]
    suffix = ", ..." if len(keys) > _MAX_KEYS_IN_ERROR else ""
    return f" — available keys: {', '.join(repr(k) for k in shown)}{suffix}"


def _descend(
    node: Any,
    seg: Segment,
    walked: list[Segment],
    next_seg: Segment | None,
    path: str,
    create_missing: bool,
) -> Any:
    """Step one segment deeper, optionally creating a missing dict on the way."""
    where = _render(walked) or "<root>"

    if isinstance(seg, int):
        if not isinstance(node, list):
            raise PatchError(
                f"cannot apply patch at {path!r}: expected a list at {where!r}, "
                f"found {type(node).__name__}"
            )
        if not -len(node) <= seg < len(node):
            raise PatchError(
                f"cannot apply patch at {path!r}: index [{seg}] is out of range at "
                f"{where!r} (list has {len(node)} item(s))"
            )
        return node[seg]

    if not isinstance(node, dict):
        raise PatchError(
            f"cannot apply patch at {path!r}: expected an object at {where!r}, "
            f"found {type(node).__name__}"
        )

    if seg in node:
        return node[seg]

    if not create_missing:
        raise PatchError(
            f"cannot apply patch at {path!r}: key {seg!r} not found at {where!r}"
            + _key_hint(node, seg)
        )
    if isinstance(next_seg, int):
        raise PatchError(
            f"cannot apply patch at {path!r}: key {seg!r} is missing at {where!r} and "
            "cannot be created because the next segment is a list index — "
            "create the list explicitly with a 'set' op first"
        )
    created: dict[str, Any] = {}
    node[seg] = created
    return created


def _resolve_parent(
    state: dict[str, Any],
    segments: list[Segment],
    path: str,
    create_missing: bool,
) -> tuple[Any, Segment]:
    """Walk to the container holding the final segment.

    Returns:
        (container, final_segment)
    """
    node: Any = state
    for i, seg in enumerate(segments[:-1]):
        next_seg = segments[i + 1]
        node = _descend(node, seg, segments[:i], next_seg, path, create_missing)
    return node, segments[-1]


def _read(container: Any, seg: Segment) -> tuple[Any, bool]:
    """Read the value at *seg* in *container*. Returns (value, existed)."""
    if isinstance(seg, int):
        if isinstance(container, list) and -len(container) <= seg < len(container):
            return container[seg], True
        return None, False
    if isinstance(container, dict) and seg in container:
        return container[seg], True
    return None, False


# ── Ops ───────────────────────────────────────────────────────────────────────


def _write(container: Any, seg: Segment, value: Any, path: str) -> None:
    """Write *value* at *seg* in *container*, validating the container type."""
    if isinstance(seg, int):
        if not isinstance(container, list):
            raise PatchError(
                f"cannot apply patch at {path!r}: index [{seg}] requires a list, "
                f"found {type(container).__name__}"
            )
        if not -len(container) <= seg < len(container):
            raise PatchError(
                f"cannot apply patch at {path!r}: index [{seg}] is out of range "
                f"(list has {len(container)} item(s)) — patches edit existing "
                "positions and cannot append"
            )
        container[seg] = value
        return

    if not isinstance(container, dict):
        raise PatchError(
            f"cannot apply patch at {path!r}: key {seg!r} requires an object, "
            f"found {type(container).__name__}"
        )
    container[seg] = value


def _op_set(
    state: dict[str, Any], path: str, value: Any, create_missing: bool
) -> PatchChange:
    segments = parse_path(path)
    container, last = _resolve_parent(state, segments, path, create_missing)
    before, existed = _read(container, last)
    if not existed and isinstance(last, str) and not create_missing:
        # Setting a brand-new key is only allowed with create_missing, so that a
        # typo produces an error instead of silently adding a field.
        if isinstance(container, dict):
            raise PatchError(
                f"cannot apply patch at {path!r}: key {last!r} does not exist"
                + _key_hint(container, last)
                + " (pass create_missing=True to add new keys)"
            )
    _write(container, last, value, path)
    return PatchChange(op="set", path=path, before=before, after=value, existed=existed)


def _op_delete(state: dict[str, Any], path: str) -> PatchChange:
    segments = parse_path(path)
    container, last = _resolve_parent(state, segments, path, create_missing=False)
    before, existed = _read(container, last)
    if not existed:
        where = _render(segments[:-1]) or "<root>"
        hint = ""
        if isinstance(container, dict) and isinstance(last, str):
            hint = _key_hint(container, last)
        raise PatchError(
            f"cannot delete {path!r}: nothing found at {where!r}{hint}"
        )
    if isinstance(last, int):
        if not isinstance(container, list):
            raise PatchError(
                f"cannot delete {path!r}: index [{last}] requires a list, "
                f"found {type(container).__name__}"
            )
        del container[last]
    else:
        del container[last]
    return PatchChange(op="delete", path=path, before=before, after=None, existed=True)


def _op_merge(
    state: dict[str, Any], path: str, value: Any, create_missing: bool
) -> PatchChange:
    if not isinstance(value, dict):
        raise PatchError(
            f"cannot merge at {path!r}: merge values must be objects, "
            f"got {type(value).__name__} — use 'set' to replace a non-object value"
        )
    segments = parse_path(path)
    container, last = _resolve_parent(state, segments, path, create_missing)
    before, existed = _read(container, last)

    if not existed:
        if not create_missing:
            hint = (
                _key_hint(container, last)
                if isinstance(container, dict) and isinstance(last, str)
                else ""
            )
            raise PatchError(
                f"cannot merge at {path!r}: nothing found to merge into{hint} "
                "(pass create_missing=True to create it)"
            )
        merged: dict[str, Any] = dict(value)
    else:
        if not isinstance(before, dict):
            raise PatchError(
                f"cannot merge at {path!r}: expected an object to merge into, "
                f"found {type(before).__name__} — use 'set' to replace it"
            )
        merged = {**before, **value}

    _write(container, last, merged, path)
    return PatchChange(
        op="merge", path=path, before=before, after=merged, existed=existed
    )


# ── Validation ────────────────────────────────────────────────────────────────


def validate_patch(patch: Any) -> None:
    """Validate a patch document's shape without applying it.

    Raises:
        PatchError: if the document is malformed.
    """
    if not isinstance(patch, dict):
        raise PatchError(
            f"patch must be a JSON object, got {type(patch).__name__}"
        )

    unknown = sorted(set(patch) - _VALID_OPS)
    if unknown:
        raise PatchError(
            f"unknown patch op(s): {', '.join(repr(k) for k in unknown)} — "
            f"valid ops are {', '.join(sorted(_VALID_OPS))}"
        )

    for op in ("set", "merge"):
        if op not in patch:
            continue
        body = patch[op]
        if not isinstance(body, dict):
            raise PatchError(
                f"patch op {op!r} must be an object mapping paths to values, "
                f"got {type(body).__name__}"
            )
        for raw_path in body:
            if not isinstance(raw_path, str):
                raise PatchError(
                    f"patch op {op!r} has a non-string path: {raw_path!r}"
                )
            parse_path(raw_path)
        if op == "merge":
            for raw_path, value in body.items():
                if not isinstance(value, dict):
                    raise PatchError(
                        f"cannot merge at {raw_path!r}: merge values must be objects, "
                        f"got {type(value).__name__} — use 'set' to replace a "
                        "non-object value"
                    )

    if "delete" in patch:
        _normalize_deletes(patch["delete"])

    _check_conflicts(patch)


def _normalize_deletes(body: Any) -> list[str]:
    """Coerce the 'delete' op body to a list of validated path strings."""
    if isinstance(body, str):
        paths = [body]
    elif isinstance(body, list):
        paths = []
        for item in body:
            if not isinstance(item, str):
                raise PatchError(
                    f"patch op 'delete' must contain path strings, "
                    f"got {type(item).__name__}: {item!r}"
                )
            paths.append(item)
    else:
        raise PatchError(
            "patch op 'delete' must be a list of paths (or a single path string), "
            f"got {type(body).__name__}"
        )
    for p in paths:
        parse_path(p)
    return paths


def _check_conflicts(patch: dict[str, Any]) -> None:
    """Reject a path targeted by more than one op group.

    Paths are compared after parsing, so ``a[0]`` and ``a.[0]`` are recognised
    as the same target.
    """
    seen: dict[tuple[Segment, ...], tuple[str, str]] = {}
    for op in _APPLY_ORDER:
        if op not in patch:
            continue
        if op == "delete":
            paths = _normalize_deletes(patch[op])
        else:
            paths = list(patch[op])
        for raw_path in paths:
            key = tuple(parse_path(raw_path))
            if key in seen:
                prev_op, prev_path = seen[key]
                raise PatchError(
                    f"conflicting patch ops: {prev_path!r} in {prev_op!r} and "
                    f"{raw_path!r} in {op!r} target the same path — "
                    "use a single op per path"
                )
            seen[key] = (op, raw_path)


# ── Public entry points ───────────────────────────────────────────────────────


def _apply(
    state: dict[str, Any], patch: dict[str, Any], create_missing: bool
) -> tuple[dict[str, Any], list[PatchChange]]:
    """Validate, deep-copy, and apply — the shared core of apply/preview."""
    if not isinstance(state, dict):
        raise PatchError(
            f"state must be a dict to be patched, got {type(state).__name__}"
        )
    validate_patch(patch)

    try:
        working = copy.deepcopy(state)
    except Exception as exc:  # pragma: no cover - exotic un-copyable states
        raise PatchError(f"state could not be copied for patching: {exc}") from exc

    changes: list[PatchChange] = []
    for op in _APPLY_ORDER:
        if op not in patch:
            continue
        if op == "delete":
            for raw_path in _normalize_deletes(patch[op]):
                changes.append(_op_delete(working, raw_path))
        elif op == "set":
            for raw_path, value in patch[op].items():
                changes.append(_op_set(working, raw_path, value, create_missing))
        else:
            for raw_path, value in patch[op].items():
                changes.append(_op_merge(working, raw_path, value, create_missing))

    return working, changes


def apply_patch(
    state: dict[str, Any],
    patch: dict[str, Any],
    create_missing: bool = False,
) -> dict[str, Any]:
    """Apply *patch* to *state* and return a new dict.

    The input state is never mutated.  Ops run in a fixed order — delete,
    then set, then merge — so the result does not depend on key ordering.

    Args:
        state: the state dict to patch (typically a recorded ``input_state``).
        patch: the patch document.
        create_missing: allow ``set``/``merge`` to create keys that do not
            exist yet.  Off by default so a mistyped path is an error rather
            than a silently added field.

    Raises:
        PatchError: if the patch is malformed or does not apply cleanly.
    """
    patched, _ = _apply(state, patch, create_missing)
    return patched


def preview_patch(
    state: dict[str, Any],
    patch: dict[str, Any],
    create_missing: bool = False,
) -> list[PatchChange]:
    """Return the changes *patch* would make to *state*, without keeping them.

    Raises the same errors as :func:`apply_patch`, so a preview doubles as a
    dry run.
    """
    _, changes = _apply(state, patch, create_missing)
    return changes


def format_changes(changes: list[PatchChange], max_value_chars: int = 120) -> list[str]:
    """Render changes as human-readable one-liners for CLI/UI display."""
    lines: list[str] = []
    for change in changes:
        if change.op == "delete":
            lines.append(f"- {change.path}  (was {_brief(change.before, max_value_chars)})")
        elif not change.existed:
            lines.append(f"+ {change.path}  = {_brief(change.after, max_value_chars)}")
        else:
            lines.append(
                f"~ {change.path}  "
                f"{_brief(change.before, max_value_chars)} -> "
                f"{_brief(change.after, max_value_chars)}"
            )
    return lines


def _brief(value: Any, max_chars: int) -> str:
    """Compact, never-raising repr of a value for display."""
    try:
        import json

        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        text = repr(value)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "\u2026"
    return text
