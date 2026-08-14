"""Signature effectiveness tracking and false-positive dispute management.

Closes the learning loop: after a learned/shared signature is approved
and loaded into the registry, this module tracks how often it fires,
across how many runs/nodes, and whether users flag hits as false positives.

Stats are computed on-demand by scanning stored run files, and also
maintained incrementally via hit metadata on custom signatures.

Disputes are stored in `.argus/signature_disputes.json`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class SignatureStats:
    """Effectiveness stats for a single signature."""

    sig_id: str
    source: str  # "builtin" | "learned" | "shared"
    description: str
    total_hits: int = 0
    runs_hit: int = 0
    nodes_hit: int = 0
    first_hit: str = ""
    last_hit: str = ""
    hit_nodes: list[str] = field(default_factory=list)
    false_positive_count: int = 0
    disabled: bool = False


# ── Dispute storage ──────────────────────────────────────────────────────────


def _disputes_path() -> Path:
    from argus.storage import argus_dir  # noqa: PLC0415

    return argus_dir() / "signature_disputes.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_disputes() -> list[dict[str, Any]]:
    """Load all disputes from disk."""
    try:
        data = json.loads(_disputes_path().read_text(encoding="utf-8"))
        return data.get("disputes", [])
    except Exception:
        return []


def _save_disputes(disputes: list[dict[str, Any]]) -> None:
    path = _disputes_path()
    _ensure_parent(path)
    path.write_text(
        json.dumps({"disputes": disputes}, indent=2),
        encoding="utf-8",
    )


def record_dispute(
    sig_id: str,
    run_id: str,
    node_name: str,
    field_path: str,
    evidence: str,
    reason: str,
) -> str:
    """Record a false-positive dispute for a signature hit.

    Returns the dispute ID.
    """
    disputes = load_disputes()
    now = datetime.now(timezone.utc).isoformat()
    dispute_id = f"disp-{uuid.uuid4().hex[:8]}"

    disputes.append(
        {
            "id": dispute_id,
            "sig_id": sig_id,
            "run_id": run_id,
            "node_name": node_name,
            "field_path": field_path,
            "evidence": evidence,
            "reason": reason,
            "created_at": now,
            "status": "confirmed",
        }
    )
    _save_disputes(disputes)
    return dispute_id


def dismiss_dispute(dispute_id: str) -> bool:
    """Remove a dispute by ID. Returns True if found and removed."""
    disputes = load_disputes()
    for i, d in enumerate(disputes):
        if d["id"] == dispute_id:
            disputes.pop(i)
            _save_disputes(disputes)
            return True
    return False


# ── Stats computation ────────────────────────────────────────────────────────


def _get_sig_source(sig_id: str) -> str:
    """Infer source from sig_id prefix convention."""
    if sig_id.startswith("CS-"):
        return "learned"
    if sig_id.startswith("SH-"):
        return "shared"
    return "builtin"


def _get_sig_description(sig_id: str) -> str:
    """Look up description from the registry."""
    from argus.registry import get_registry  # noqa: PLC0415

    for sig in get_registry():
        if sig.get("id") == sig_id:
            return sig.get("description", "")
    return ""


def _is_sig_disabled(sig_id: str) -> bool:
    """Check if a custom signature is disabled."""
    from argus.candidate_store import load_custom_signatures  # noqa: PLC0415

    custom = load_custom_signatures()
    for sig in custom.get("signatures", []):
        if sig["id"] == sig_id:
            return bool(sig.get("metadata", {}).get("disabled", False))
    return False


def compute_stats(
    sig_ids: list[str] | None = None,
    include_builtins: bool = False,
) -> dict[str, SignatureStats]:
    """Compute effectiveness stats by scanning all stored runs.

    Args:
        sig_ids: If provided, only compute stats for these sig IDs.
        include_builtins: If True, include builtin signatures in results.

    Returns:
        Dict mapping sig_id to SignatureStats.
    """
    from argus.storage import runs_dir as _resolved_runs_dir  # noqa: PLC0415

    runs_dir = _resolved_runs_dir(create=False)
    if not runs_dir.exists():
        return {}

    # Collect hits: sig_id -> {run_ids, node_names, timestamps}
    hits: dict[str, dict[str, Any]] = {}

    for run_file in runs_dir.glob("*.json"):
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        run_id = data.get("run_id", run_file.stem)
        started_at = data.get("started_at", "")

        for step in data.get("steps", []):
            inspection = step.get("inspection")
            if not inspection:
                continue

            for signal in inspection.get("semantic_signals", []):
                sid = signal.get("sig_id", "")
                if not sid:
                    continue

                # Filter by requested sig_ids
                if sig_ids is not None and sid not in sig_ids:
                    continue

                # Filter builtins unless requested
                if not include_builtins and _get_sig_source(sid) == "builtin":
                    continue

                if sid not in hits:
                    hits[sid] = {
                        "run_ids": set(),
                        "node_names": set(),
                        "timestamps": [],
                        "count": 0,
                    }

                hits[sid]["run_ids"].add(run_id)
                hits[sid]["node_names"].add(step.get("node_name", ""))
                hits[sid]["timestamps"].append(started_at)
                hits[sid]["count"] += 1

    # Load disputes for FP counts
    disputes = load_disputes()
    fp_counts: dict[str, int] = {}
    for d in disputes:
        sid = d.get("sig_id", "")
        fp_counts[sid] = fp_counts.get(sid, 0) + 1

    # Build stats
    result: dict[str, SignatureStats] = {}

    for sid, h in hits.items():
        timestamps = sorted(h["timestamps"])
        node_list = sorted(h["node_names"])

        result[sid] = SignatureStats(
            sig_id=sid,
            source=_get_sig_source(sid),
            description=_get_sig_description(sid),
            total_hits=h["count"],
            runs_hit=len(h["run_ids"]),
            nodes_hit=len(h["node_names"]),
            first_hit=timestamps[0] if timestamps else "",
            last_hit=timestamps[-1] if timestamps else "",
            hit_nodes=node_list,
            false_positive_count=fp_counts.get(sid, 0),
            disabled=_is_sig_disabled(sid),
        )

    # Include sigs with zero hits if specific IDs were requested
    if sig_ids:
        for sid in sig_ids:
            if sid not in result:
                result[sid] = SignatureStats(
                    sig_id=sid,
                    source=_get_sig_source(sid),
                    description=_get_sig_description(sid),
                    false_positive_count=fp_counts.get(sid, 0),
                    disabled=_is_sig_disabled(sid),
                )

    return result


# ── Incremental hit tracking (called from storage.save_run) ──────────────────


def update_hit_metadata(run_data: dict[str, Any]) -> None:
    """Update custom_signatures.json hit metadata from a completed run.

    Called by storage.save_run() after writing the run file. Scans the
    run's semantic_signals and increments total_hits / last_hit_at for
    any CS- or SH- signatures found.
    """
    from argus.candidate_store import (  # noqa: PLC0415
        load_custom_signatures,
        save_custom_signatures,
    )

    # Collect sig_ids that fired in this run
    fired: set[str] = set()
    for step in run_data.get("steps", []):
        inspection = step.get("inspection")
        if not inspection:
            continue
        for signal in inspection.get("semantic_signals", []):
            sid = signal.get("sig_id", "")
            if sid.startswith(("CS-", "SH-")):
                fired.add(sid)

    if not fired:
        return

    custom = load_custom_signatures()
    modified = False
    now = datetime.now(timezone.utc).isoformat()

    for sig in custom.get("signatures", []):
        if sig["id"] in fired:
            meta = sig.setdefault("metadata", {})
            meta["total_hits"] = meta.get("total_hits", 0) + 1
            meta["last_hit_at"] = now
            modified = True

    if modified:
        save_custom_signatures(custom)


# ── Auto-pruning ────────────────────────────────────────────────────────────

# Thresholds
GRACE_PERIOD_DAYS = 14  # Don't judge newly approved signatures
ZERO_HIT_PRUNE_DAYS = 14  # After grace: 0 catches → removed
STALE_DAYS = 30  # Last catch was this long ago
MIN_HITS_TO_KEEP = 3  # Signatures with 3+ catches are kept forever


def prune_stale_signatures() -> list[str]:
    """Auto-remove signatures that haven't proven useful.

    Rules (applied only after GRACE_PERIOD_DAYS since approval):
    1. Zero catches → removed
    2. Last catch older than STALE_DAYS and fewer than MIN_HITS_TO_KEEP → removed
    3. Signatures with MIN_HITS_TO_KEEP+ total catches are kept permanently

    Returns list of removed signature IDs.
    """
    from argus.candidate_store import (  # noqa: PLC0415
        load_custom_signatures,
        save_custom_signatures,
    )

    custom = load_custom_signatures()
    sigs = custom.get("signatures", [])
    if not sigs:
        return []

    now = datetime.now(timezone.utc)
    to_remove: list[str] = []

    for sig in sigs:
        meta = sig.get("metadata", {})
        approved_at = meta.get("approved_at")
        if not approved_at:
            continue

        try:
            approved_dt = datetime.fromisoformat(approved_at)
            if approved_dt.tzinfo is None:
                approved_dt = approved_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        days_since_approval = (now - approved_dt).days
        if days_since_approval < GRACE_PERIOD_DAYS:
            continue

        total_hits = meta.get("total_hits", 0)

        # Rule 1: zero catches after grace period
        if total_hits == 0:
            to_remove.append(sig["id"])
            continue

        # Rule 2: stale — last catch too old and not enough total hits
        if total_hits < MIN_HITS_TO_KEEP:
            last_hit_at = meta.get("last_hit_at")
            if last_hit_at:
                try:
                    last_dt = datetime.fromisoformat(last_hit_at)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    if (now - last_dt).days > STALE_DAYS:
                        to_remove.append(sig["id"])
                except (ValueError, TypeError):
                    pass

    if not to_remove:
        return []

    # Remove in one pass
    custom["signatures"] = [s for s in sigs if s["id"] not in to_remove]
    save_custom_signatures(custom)
    return to_remove
