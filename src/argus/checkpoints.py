"""Checkpoint persistence for interrupted (human-approval) runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from argus.storage import argus_dir

_CHECKPOINTS_DIR = "checkpoints"


def _checkpoints_path() -> Path:
    base = argus_dir() / _CHECKPOINTS_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class CheckpointRecord:
    run_id: str
    interrupted_at_node: str
    checkpoint_state: dict[str, Any]
    created_at: str
    resumed: bool = False
    resumed_at: str | None = None


def save_checkpoint(record: CheckpointRecord) -> Path:
    """Write a checkpoint to .argus/checkpoints/<run_id>.json atomically."""
    path = _checkpoints_path() / f"{record.run_id}.json"
    tmp = path.with_suffix(".tmp")
    data = {
        "run_id": record.run_id,
        "interrupted_at_node": record.interrupted_at_node,
        "checkpoint_state": record.checkpoint_state,
        "created_at": record.created_at,
        "resumed": record.resumed,
        "resumed_at": record.resumed_at,
    }
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.rename(path)
    return path


def load_checkpoint(run_id: str) -> CheckpointRecord | None:
    """Load a checkpoint by run_id. Returns None if not found."""
    path = _checkpoints_path() / f"{run_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return CheckpointRecord(
        run_id=data["run_id"],
        interrupted_at_node=data["interrupted_at_node"],
        checkpoint_state=data.get("checkpoint_state", {}),
        created_at=data.get("created_at", ""),
        resumed=data.get("resumed", False),
        resumed_at=data.get("resumed_at"),
    )


def mark_checkpoint_resumed(run_id: str) -> None:
    """Update the checkpoint file: set resumed=True and resumed_at=now."""
    record = load_checkpoint(run_id)
    if record is None:
        return
    record.resumed = True
    record.resumed_at = datetime.now(timezone.utc).isoformat()
    save_checkpoint(record)
