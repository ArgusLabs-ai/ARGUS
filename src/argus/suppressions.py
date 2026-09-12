"""Project-level signature suppressions — ``argus ignore``.

A suppression silences one signature id (``NL-002``, ``BA-005``, ``PH-007`` …)
either everywhere in the project or on one node. Suppressed hits are **not
dropped**: they are moved off the inspection (so they no longer affect the
node's status or the run verdict) and recorded on the event under
``suppressed_signals`` / ``suppressed_anomalies`` so ``argus stats`` and the
findings list can still count them.

Stored in ``<project-root>/.argus/config.json`` so a team shares one list::

    {"suppressions": [{"id": "NL-002", "node": null},
                      {"id": "RF-001", "node": "draft_hook"}]}
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from argus.storage import argus_dir

_CONFIG_FILE = "config.json"


@dataclass(frozen=True)
class Suppression:
    id: str
    node: str | None = None  # None = every node

    def matches(self, sig_id: str, node: str | None) -> bool:
        if self.id.upper() != sig_id.upper():
            return False
        return self.node is None or self.node == node

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "node": self.node}

    @property
    def label(self) -> str:
        return f"{self.id} on {self.node}" if self.node else f"{self.id} on every node"


def config_path(start: Path | None = None) -> Path:
    return argus_dir(start) / _CONFIG_FILE


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_suppressions(start: Path | None = None) -> list[Suppression]:
    raw = _read(config_path(start)).get("suppressions", [])
    out: list[Suppression] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            out.append(Suppression(id=item))
        elif isinstance(item, dict) and item.get("id"):
            out.append(Suppression(id=str(item["id"]), node=item.get("node") or None))
    return out


def _save(items: list[Suppression], start: Path | None = None) -> None:
    path = config_path(start)
    data = _read(path)
    data["suppressions"] = [s.as_dict() for s in items]
    _write(path, data)


def add_suppression(sig_id: str, node: str | None = None, start: Path | None = None) -> bool:
    """Add a suppression. Returns False if it already existed."""
    sig_id = sig_id.strip().upper()
    if not sig_id:
        raise ValueError("signature id is required")
    items = load_suppressions(start)
    new = Suppression(id=sig_id, node=node or None)
    if new in items:
        return False
    items.append(new)
    _save(items, start)
    return True


def remove_suppression(sig_id: str, node: str | None = None, start: Path | None = None) -> bool:
    """Remove one suppression. Returns False if it was not present."""
    target = Suppression(id=sig_id.strip().upper(), node=node or None)
    items = load_suppressions(start)
    if target not in items:
        return False
    _save([s for s in items if s != target], start)
    return True


def is_suppressed(sig_id: str, node: str | None, suppressions: Sequence[Suppression]) -> bool:
    return any(s.matches(sig_id, node) for s in suppressions)


_S = TypeVar("_S")


def split_suppressed(
    signals: Sequence[_S],
    node: str | None,
    suppressions: Sequence[Suppression],
    *,
    id_attr: str,
) -> tuple[list[_S], list[_S]]:
    """Partition ``signals`` into (kept, suppressed) by the id in ``id_attr``."""
    if not suppressions:
        return list(signals), []
    kept: list[_S] = []
    dropped: list[_S] = []
    for s in signals:
        if is_suppressed(getattr(s, id_attr, ""), node, suppressions):
            dropped.append(s)
        else:
            kept.append(s)
    return kept, dropped
