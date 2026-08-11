"""Candidate signature storage for the adaptive learning loop.

Manages two files in `.argus/`:
- `candidates.json`  — pending patterns awaiting developer review
- `custom_signatures.json` — approved patterns loaded by the registry
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from argus.models import SuggestedSignature

_CANDIDATES_PATH = Path(".argus/candidates.json")
_CUSTOM_SIGS_PATH = Path(".argus/custom_signatures.json")


# ── File I/O ──────────────────────────────────────────────────────────────────


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_candidates() -> dict[str, Any]:
    try:
        return json.loads(_CANDIDATES_PATH.read_text())
    except Exception:
        return {"candidates": [], "rejected_patterns": []}


def save_candidates(data: dict[str, Any]) -> None:
    _ensure_parent(_CANDIDATES_PATH)
    _CANDIDATES_PATH.write_text(json.dumps(data, indent=2))


def load_custom_signatures() -> dict[str, Any]:
    try:
        return json.loads(_CUSTOM_SIGS_PATH.read_text())
    except Exception:
        return {"version": "1.0.0", "signatures": []}


def save_custom_signatures(data: dict[str, Any]) -> None:
    _ensure_parent(_CUSTOM_SIGS_PATH)
    _CUSTOM_SIGS_PATH.write_text(json.dumps(data, indent=2))


# ── Dedup helpers ─────────────────────────────────────────────────────────────


def _pattern_in_registry(pattern: str, strategy: str) -> bool:
    """Check if a pattern already exists in the bundled registry."""
    from argus.registry import get_registry  # noqa: PLC0415

    for sig in get_registry():
        if sig.get("pattern") == pattern and sig.get("match_strategy") == strategy:
            return True
    return False


def _pattern_in_custom(pattern: str, strategy: str, custom: dict[str, Any]) -> bool:
    for sig in custom.get("signatures", []):
        if sig.get("pattern") == pattern and sig.get("match_strategy") == strategy:
            return True
    return False


# ── Core operations ───────────────────────────────────────────────────────────


def add_candidate(
    sig: SuggestedSignature,
    run_id: str,
    node_name: str = "",
) -> str | None:
    """Queue a suggested signature as a candidate for review.

    Returns the candidate ID, or None if skipped (duplicate, rejected, or
    already in the registry).
    """
    data = load_candidates()
    now = datetime.now(timezone.utc).isoformat()

    # Skip if pattern was previously rejected
    if sig.pattern in data.get("rejected_patterns", []):
        return None

    # Skip if already in bundled or custom registry
    if _pattern_in_registry(sig.pattern, sig.match_strategy):
        return None
    if _pattern_in_custom(sig.pattern, sig.match_strategy, load_custom_signatures()):
        return None

    # Dedup: if same pattern+strategy already pending, merge
    for cand in data["candidates"]:
        if cand["pattern"] == sig.pattern and cand["match_strategy"] == sig.match_strategy:
            cand["times_seen"] = cand.get("times_seen", 1) + 1
            cand["last_seen"] = now
            if run_id and run_id not in cand.get("source_run_ids", []):
                cand.setdefault("source_run_ids", []).append(run_id)
            if node_name and node_name not in cand.get("source_nodes", []):
                cand.setdefault("source_nodes", []).append(node_name)
            save_candidates(data)
            return cand["id"]

    # New candidate
    cand_id = f"cand-{uuid.uuid4().hex[:8]}"
    candidate = {
        "id": cand_id,
        "pattern": sig.pattern,
        "match_strategy": sig.match_strategy,
        "proposed_category": sig.proposed_category,
        "severity": sig.severity,
        "description": sig.description,
        "evidence": list(sig.evidence),
        "confidence": sig.confidence,
        "reasoning": sig.reasoning,
        "source_run_ids": [run_id] if run_id else [],
        "source_nodes": [node_name] if node_name else [],
        "times_seen": 1,
        "first_seen": now,
        "last_seen": now,
        "status": "pending",
        "original_pattern": getattr(sig, "original_pattern", None),
        "generalized": getattr(sig, "generalized", False),
    }
    data["candidates"].append(candidate)
    save_candidates(data)
    return cand_id


def approve_candidate(candidate_id: str) -> dict[str, Any] | None:
    """Move a candidate to custom_signatures.json. Returns the new signature."""
    data = load_candidates()
    cand = None
    for i, c in enumerate(data["candidates"]):
        if c["id"] == candidate_id:
            cand = data["candidates"].pop(i)
            break
    if cand is None:
        return None

    save_candidates(data)

    custom = load_custom_signatures()
    # Assign next CS-NNN id
    existing_ids = [
        int(s["id"].split("-")[1])
        for s in custom.get("signatures", [])
        if s.get("id", "").startswith("CS-") and s["id"].split("-")[1].isdigit()
    ]
    next_num = max(existing_ids, default=0) + 1
    sig_id = f"CS-{next_num:03d}"

    now = datetime.now(timezone.utc).isoformat()
    new_sig = {
        "id": sig_id,
        "category": cand["proposed_category"],
        "pattern": cand["pattern"],
        "match_strategy": cand["match_strategy"],
        "severity": cand["severity"],
        "description": cand["description"],
        "source": "learned",
        "metadata": {
            "confidence": cand.get("confidence"),
            "frequency": cand.get("times_seen", 1),
            "approval_status": "approved",
            "approved_at": now,
            "framework_specific": None,
            "original_pattern": cand.get("original_pattern"),
            "generalized": cand.get("generalized", False),
        },
    }
    custom["signatures"].append(new_sig)
    save_custom_signatures(custom)
    return new_sig


def reject_candidate(candidate_id: str) -> bool:
    """Remove a candidate and add its pattern to the rejected list."""
    data = load_candidates()
    for i, c in enumerate(data["candidates"]):
        if c["id"] == candidate_id:
            pattern = c["pattern"]
            data["candidates"].pop(i)
            if pattern not in data.get("rejected_patterns", []):
                data.setdefault("rejected_patterns", []).append(pattern)
            save_candidates(data)
            return True
    return False


def approve_candidate_shared(candidate_id: str) -> dict[str, Any] | None:
    """Approve a candidate and push it to the shared community registry.

    The candidate is removed from the local pending list and pushed to
    Supabase so all users benefit from the pattern. Requires the user
    to be logged in (``argus login``).

    Returns the signature dict on success, or None if the candidate was
    not found or the push failed.

    In open-source / local-only mode (no ARGUS_SUPABASE_URL configured) there
    is no shared community registry, so this transparently falls back to a
    local approval — every trend stays on the user's machine.
    """
    from argus.cloud import SUPABASE_URL  # noqa: PLC0415

    if SUPABASE_URL is None:
        return approve_candidate(candidate_id)

    data = load_candidates()
    cand = None
    for i, c in enumerate(data["candidates"]):
        if c["id"] == candidate_id:
            cand = data["candidates"].pop(i)
            break
    if cand is None:
        return None

    save_candidates(data)

    # Build the signature in the same format as approve_candidate
    now = datetime.now(timezone.utc).isoformat()
    sig_id = f"SH-{uuid.uuid4().hex[:6].upper()}"

    new_sig: dict[str, Any] = {
        "id": sig_id,
        "category": cand["proposed_category"],
        "pattern": cand["pattern"],
        "match_strategy": cand["match_strategy"],
        "severity": cand["severity"],
        "description": cand["description"],
        "source": "shared",
        "source_run_ids": cand.get("source_run_ids", []),
        "source_nodes": cand.get("source_nodes", []),
        "reasoning": cand.get("reasoning", ""),
        "metadata": {
            "confidence": cand.get("confidence"),
            "frequency": cand.get("times_seen", 1),
            "approval_status": "approved",
            "approved_at": now,
            "framework_specific": None,
            "original_pattern": cand.get("original_pattern"),
            "generalized": cand.get("generalized", False),
        },
    }

    from argus.cloud import push_shared_signature  # noqa: PLC0415

    if not push_shared_signature(new_sig):
        # Push failed — re-add candidate so it's not lost
        data = load_candidates()
        data["candidates"].append(cand)
        save_candidates(data)
        return None

    # Pull back the full community registry so the newly approved signature
    # (and any others contributed since last sync) is immediately active.
    try:
        from argus.registry import sync_shared_signatures  # noqa: PLC0415

        sync_shared_signatures()
    except Exception:
        pass

    return new_sig


def delete_custom_signature(sig_id: str) -> bool:
    """Remove an approved custom signature."""
    custom = load_custom_signatures()
    for i, s in enumerate(custom.get("signatures", [])):
        if s["id"] == sig_id:
            custom["signatures"].pop(i)
            save_custom_signatures(custom)
            return True
    return False


def disable_custom_signature(sig_id: str) -> bool:
    """Disable a custom signature without deleting it.

    The signature stays in custom_signatures.json but is skipped
    by the registry loader. Returns False if sig_id not found.
    """
    custom = load_custom_signatures()
    for s in custom.get("signatures", []):
        if s["id"] == sig_id:
            s.setdefault("metadata", {})["disabled"] = True
            save_custom_signatures(custom)
            return True
    return False


def enable_custom_signature(sig_id: str) -> bool:
    """Re-enable a previously disabled custom signature."""
    custom = load_custom_signatures()
    for s in custom.get("signatures", []):
        if s["id"] == sig_id:
            s.setdefault("metadata", {}).pop("disabled", None)
            save_custom_signatures(custom)
            return True
    return False
