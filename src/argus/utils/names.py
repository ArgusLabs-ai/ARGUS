"""The naming rule for anything shape-only (the run card, ``argus fix --sanitized``).

A node or tool name is chosen in code, so it may be shown as written. A state key or a
field path is not always: a dict can be keyed by the very value a privacy rule protects
(``{"123-45-6789": ...}``). A key is therefore shown only when it *looks like an identifier*
(or a list index); anything else becomes ``?``.

The regular expressions here are mirrored in ``data/cloud_card.schema.json``; a test checks
that every name this module can produce validates against it.
"""

from __future__ import annotations

import re
from typing import Any

UNKNOWN = "?"
MAX_SEGMENTS = 8

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# One dotted segment: an optional identifier, then any number of list indices ("items[0]", "[0]").
_SEGMENT = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]{0,63})?(?:\[[0-9]{1,6}\])*")


def safe_key(key: Any) -> str:
    """``key`` when it is text that looks like an identifier, else ``?``."""
    return key if isinstance(key, str) and _IDENTIFIER.fullmatch(key) else UNKNOWN


def safe_path(path: Any) -> str | None:
    """A dotted field path with each segment judged on its own; ``None`` for no path.

    ``account.<value>.balance`` becomes ``account.?.balance``. At most
    ``MAX_SEGMENTS`` segments are kept.
    """
    if path is None or path == "":
        return None
    parts = (path if isinstance(path, str) else str(path)).split(".")[:MAX_SEGMENTS]
    return ".".join(p if p and _SEGMENT.fullmatch(p) else UNKNOWN for p in parts)
