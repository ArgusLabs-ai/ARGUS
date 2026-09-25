"""Tests for signature effectiveness tracking (learning loop closure)."""

import json

import pytest

from argus.candidate_store import (
    disable_custom_signature,
    enable_custom_signature,
    load_custom_signatures,
    save_custom_signatures,
)
from argus.signature_stats import (
    GRACE_PERIOD_DAYS,
    MIN_HITS_TO_KEEP,
    STALE_DAYS,
    compute_stats,
    dismiss_dispute,
    load_disputes,
    prune_stale_signatures,
    record_dispute,
    update_hit_metadata,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Run every test in a temp directory."""
    monkeypatch.chdir(tmp_path)


def _make_run_file(tmp_path, run_id, signals, started_at="2026-07-01T00:00:00Z"):
    """Create a minimal run JSON with the given semantic_signals."""
    runs_dir = tmp_path / ".argus" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run = {
        "run_id": run_id,
        "started_at": started_at,
        "steps": [
            {
                "node_name": sig["node_name"],
                "inspection": {
                    "semantic_signals": [
                        {
                            "sig_id": sig["sig_id"],
                            "category": sig.get("category", "test"),
                            "severity": sig.get("severity", "warning"),
                            "description": sig.get("description", "test signal"),
                            "field_path": ["output"],
                            "evidence": sig.get("evidence", "test"),
                        }
                    ],
                },
            }
            for sig in signals
        ],
    }
    path = runs_dir / f"{run_id}.json"
    path.write_text(json.dumps(run, indent=2))
    return path


def _setup_custom_sig(sig_id="CS-001"):
    """Create a custom_signatures.json with one signature."""
    from pathlib import Path

    argus_dir = Path(".argus")
    argus_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": "1.0.0",
        "signatures": [
            {
                "id": sig_id,
                "category": "test_category",
                "pattern": "test pattern",
                "match_strategy": "contains_ci",
                "severity": "warning",
                "description": "A test learned signature",
                "source": "learned",
                "metadata": {
                    "confidence": 0.9,
                    "frequency": 1,
                    "approval_status": "approved",
                },
            }
        ],
    }
    save_custom_signatures(data)


# ── compute_stats tests ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_compute_stats_empty():
    """No runs dir → empty stats."""
    result = compute_stats()
    assert result == {}


@pytest.mark.unit
def test_compute_stats_counts_hits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_run_file(
        tmp_path,
        "run-001",
        [
            {"sig_id": "CS-001", "node_name": "summarizer"},
            {"sig_id": "CS-001", "node_name": "classifier"},
        ],
        started_at="2026-07-01T10:00:00Z",
    )
    _make_run_file(
        tmp_path,
        "run-002",
        [{"sig_id": "CS-001", "node_name": "summarizer"}],
        started_at="2026-07-02T10:00:00Z",
    )

    result = compute_stats()
    assert "CS-001" in result
    s = result["CS-001"]
    assert s.total_hits == 3
    assert s.runs_hit == 2
    assert s.nodes_hit == 2
    assert "summarizer" in s.hit_nodes
    assert "classifier" in s.hit_nodes


@pytest.mark.unit
def test_compute_stats_filters_builtins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_run_file(
        tmp_path,
        "run-001",
        [
            {"sig_id": "PH-001", "node_name": "node1"},
            {"sig_id": "CS-001", "node_name": "node1"},
        ],
    )

    # Default: no builtins
    result = compute_stats(include_builtins=False)
    assert "PH-001" not in result
    assert "CS-001" in result

    # With builtins
    result = compute_stats(include_builtins=True)
    assert "PH-001" in result
    assert "CS-001" in result


@pytest.mark.unit
def test_compute_stats_specific_sig(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_run_file(
        tmp_path,
        "run-001",
        [
            {"sig_id": "CS-001", "node_name": "a"},
            {"sig_id": "CS-002", "node_name": "b"},
        ],
    )

    result = compute_stats(sig_ids=["CS-001"])
    assert "CS-001" in result
    assert "CS-002" not in result


@pytest.mark.unit
def test_compute_stats_zero_hits_for_requested_sig(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_run_file(tmp_path, "run-001", [{"sig_id": "CS-001", "node_name": "a"}])

    result = compute_stats(sig_ids=["CS-999"])
    assert "CS-999" in result
    assert result["CS-999"].total_hits == 0


# ── Dispute tests ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_record_and_load_disputes():
    dispute_id = record_dispute(
        sig_id="CS-001",
        run_id="run-001",
        node_name="summarizer",
        field_path="output.summary",
        evidence="matched text",
        reason="this is valid output",
    )
    assert dispute_id.startswith("disp-")

    disputes = load_disputes()
    assert len(disputes) == 1
    assert disputes[0]["sig_id"] == "CS-001"
    assert disputes[0]["reason"] == "this is valid output"


@pytest.mark.unit
def test_dismiss_dispute():
    dispute_id = record_dispute(
        sig_id="CS-001",
        run_id="run-001",
        node_name="n",
        field_path="f",
        evidence="e",
        reason="r",
    )
    assert dismiss_dispute(dispute_id) is True
    assert load_disputes() == []
    assert dismiss_dispute("nonexistent") is False


@pytest.mark.unit
def test_compute_stats_includes_fp_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_run_file(tmp_path, "run-001", [{"sig_id": "CS-001", "node_name": "a"}])
    record_dispute("CS-001", "run-001", "a", "f", "e", "false positive")
    record_dispute("CS-001", "run-002", "b", "f", "e", "another fp")

    result = compute_stats()
    assert result["CS-001"].false_positive_count == 2


# ── Disable/enable tests ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_disable_enable_custom_signature():
    _setup_custom_sig("CS-001")

    assert disable_custom_signature("CS-001") is True
    custom = load_custom_signatures()
    assert custom["signatures"][0]["metadata"]["disabled"] is True

    assert enable_custom_signature("CS-001") is True
    custom = load_custom_signatures()
    assert "disabled" not in custom["signatures"][0]["metadata"]


@pytest.mark.unit
def test_disable_nonexistent():
    _setup_custom_sig("CS-001")
    assert disable_custom_signature("CS-999") is False


@pytest.mark.unit
def test_disabled_sig_skipped_by_registry():
    _setup_custom_sig("CS-001")
    disable_custom_signature("CS-001")

    from argus.registry import get_registry, reload_registry

    reload_registry()
    registry = get_registry()

    cs_ids = [s["id"] for s in registry if s["id"] == "CS-001"]
    assert cs_ids == [], "Disabled signature should not appear in registry"


@pytest.mark.unit
def test_enabled_sig_loaded_by_registry():
    _setup_custom_sig("CS-001")
    disable_custom_signature("CS-001")
    enable_custom_signature("CS-001")

    from argus.registry import get_registry, reload_registry

    reload_registry()
    registry = get_registry()

    cs_ids = [s["id"] for s in registry if s["id"] == "CS-001"]
    assert len(cs_ids) == 1, "Enabled signature should appear in registry"


# ── Hit metadata update tests ────────────────────────────────────────────────


@pytest.mark.unit
def test_update_hit_metadata():
    _setup_custom_sig("CS-001")

    run_data = {
        "steps": [
            {
                "inspection": {
                    "semantic_signals": [
                        {"sig_id": "CS-001"},
                        {"sig_id": "PH-001"},  # builtin, should be ignored
                    ]
                }
            }
        ]
    }

    update_hit_metadata(run_data)
    custom = load_custom_signatures()
    sig = custom["signatures"][0]
    assert sig["metadata"]["total_hits"] == 1
    assert "last_hit_at" in sig["metadata"]


@pytest.mark.unit
def test_update_hit_metadata_increments():
    _setup_custom_sig("CS-001")

    run_data = {
        "steps": [
            {"inspection": {"semantic_signals": [{"sig_id": "CS-001"}]}}
        ]
    }

    update_hit_metadata(run_data)
    update_hit_metadata(run_data)

    custom = load_custom_signatures()
    assert custom["signatures"][0]["metadata"]["total_hits"] == 2


@pytest.mark.unit
def test_update_hit_metadata_no_custom_sigs():
    """No crash when no custom signatures exist."""
    run_data = {
        "steps": [
            {"inspection": {"semantic_signals": [{"sig_id": "CS-001"}]}}
        ]
    }
    # Should not raise
    update_hit_metadata(run_data)


# ── Auto-prune tests ────────────────────────────────────────────────────────


def _setup_sig_with_meta(sig_id, approved_at, total_hits=0, last_hit_at=None):
    """Create a custom signature with specific metadata for prune testing."""
    from pathlib import Path

    argus_dir = Path(".argus")
    argus_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "confidence": 0.9,
        "frequency": 1,
        "approval_status": "approved",
        "approved_at": approved_at,
        "total_hits": total_hits,
    }
    if last_hit_at:
        meta["last_hit_at"] = last_hit_at

    # Append to existing or create new
    custom = load_custom_signatures()
    custom["signatures"].append(
        {
            "id": sig_id,
            "category": "test",
            "pattern": f"pattern-{sig_id}",
            "match_strategy": "contains_ci",
            "severity": "warning",
            "description": f"Test sig {sig_id}",
            "source": "learned",
            "metadata": meta,
        }
    )
    save_custom_signatures(custom)


@pytest.mark.unit
def test_prune_skips_during_grace_period():
    """Signatures within GRACE_PERIOD_DAYS are never pruned."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    recent = (now - __import__("datetime").timedelta(days=GRACE_PERIOD_DAYS - 1)).isoformat()
    _setup_sig_with_meta("CS-001", approved_at=recent, total_hits=0)

    removed = prune_stale_signatures()
    assert removed == []
    assert len(load_custom_signatures()["signatures"]) == 1


@pytest.mark.unit
def test_prune_removes_zero_hit_after_grace():
    """Signatures with 0 catches after grace period are pruned."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=GRACE_PERIOD_DAYS + 1)).isoformat()
    _setup_sig_with_meta("CS-001", approved_at=old, total_hits=0)

    removed = prune_stale_signatures()
    assert "CS-001" in removed
    assert len(load_custom_signatures()["signatures"]) == 0


@pytest.mark.unit
def test_prune_keeps_high_hit_signatures():
    """Signatures with MIN_HITS_TO_KEEP+ catches are never pruned."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    old_hit = (datetime.now(timezone.utc) - timedelta(days=STALE_DAYS + 10)).isoformat()
    _setup_sig_with_meta(
        "CS-001", approved_at=old,
        total_hits=MIN_HITS_TO_KEEP, last_hit_at=old_hit,
    )

    removed = prune_stale_signatures()
    assert removed == []
    assert len(load_custom_signatures()["signatures"]) == 1


@pytest.mark.unit
def test_prune_removes_stale_low_hit():
    """Signatures with few hits and stale last_hit are pruned."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    stale_hit = (datetime.now(timezone.utc) - timedelta(days=STALE_DAYS + 5)).isoformat()
    _setup_sig_with_meta("CS-001", approved_at=old, total_hits=1, last_hit_at=stale_hit)

    removed = prune_stale_signatures()
    assert "CS-001" in removed


@pytest.mark.unit
def test_prune_keeps_recent_low_hit():
    """Signatures with few hits but recent activity are kept."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    recent_hit = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    _setup_sig_with_meta("CS-001", approved_at=old, total_hits=1, last_hit_at=recent_hit)

    removed = prune_stale_signatures()
    assert removed == []
    assert len(load_custom_signatures()["signatures"]) == 1


@pytest.mark.unit
def test_prune_selective_removal():
    """Only stale signatures are removed; healthy ones survive."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=30)).isoformat()
    recent_hit = (now - timedelta(days=2)).isoformat()
    stale_hit = (now - timedelta(days=STALE_DAYS + 5)).isoformat()

    # pruned: zero hits
    _setup_sig_with_meta("CS-001", approved_at=old, total_hits=0)
    # kept: high hits
    _setup_sig_with_meta(
        "CS-002", approved_at=old,
        total_hits=5, last_hit_at=recent_hit,
    )
    # pruned: stale
    _setup_sig_with_meta(
        "CS-003", approved_at=old,
        total_hits=1, last_hit_at=stale_hit,
    )

    removed = prune_stale_signatures()
    assert sorted(removed) == ["CS-001", "CS-003"]

    remaining = [s["id"] for s in load_custom_signatures()["signatures"]]
    assert remaining == ["CS-002"]


@pytest.mark.unit
def test_load_disputes_corrupt_file_warns_and_returns_empty():
    """B-4 (F-16): a corrupt disputes file must warn loudly, never fail silent."""
    import warnings as _warnings

    from argus.signature_stats import _disputes_path

    _ensure = _disputes_path()
    _ensure.parent.mkdir(parents=True, exist_ok=True)
    _ensure.write_text("{not json", encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="corrupt disputes file"):
        assert load_disputes() == []


@pytest.mark.unit
def test_load_disputes_absent_file_is_silent_empty():
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        assert load_disputes() == []


@pytest.mark.unit
def test_load_disputes_non_list_disputes_warns():
    from argus.signature_stats import _disputes_path

    path = _disputes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"disputes": {"oops": "not-a-list"}}), encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="not a list"):
        assert load_disputes() == []
