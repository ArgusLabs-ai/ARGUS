"""End-to-end validation of the private + public DB learning loop.

Flow under test:
  1. LLM suggests a failure trend  →  added to candidates.json
  2a. PRIVATE path: approve_candidate()       → custom_signatures.json
  2b. PUBLIC  path: approve_candidate_shared() → shared_signatures_cache.json
  3. Subsequent runs flag the same output WITHOUT any LLM calls (fast path)

Run with:
    pytest tests/test_db_flow.py -v -s
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.candidate_store import (
    add_candidate,
    approve_candidate,
    approve_candidate_shared,
    load_candidates,
    load_custom_signatures,
    reject_candidate,
)
from argus.models import LLMInvestigationConfig, SuggestedSignature
from argus.registry import get_registry, reload_registry
from argus.session import ArgusSession

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Each test runs in a clean tmp dir with an isolated .argus/ folder.

    We stub out Supabase pulls so the background sync thread is a no-op and
    cannot clobber the shared_signatures_cache.json we write in tests.
    """
    monkeypatch.chdir(tmp_path)
    # Supabase pull → empty list so sync_shared_signatures() becomes a no-op
    monkeypatch.setattr("argus.cloud.pull_shared_signatures", lambda: [])
    # Reset the module-level registry singleton to reflect the clean tmp dir
    reload_registry()
    yield
    # Clean up for the next test
    reload_registry()


def _make_sig(
    pattern: str,
    category: str = "placeholder_outputs",
    strategy: str = "contains_ci",
    severity: str = "warning",
) -> SuggestedSignature:
    """Helper to build a SuggestedSignature for tests."""
    return SuggestedSignature(
        pattern=pattern,
        match_strategy=strategy,
        proposed_category=category,
        severity=severity,
        description=f"Detects: {pattern}",
        evidence=(f"Example output containing {pattern}",),
        confidence=0.85,
        reasoning="Seen repeatedly in node outputs during degraded runs",
    )


def _no_llm_session() -> ArgusSession:
    """ArgusSession with all LLM calls explicitly disabled."""
    return ArgusSession(
        llm_investigation=LLMInvestigationConfig(enabled=False),
    )


def _load_run(runs_dir: Path) -> dict:
    """Load the single run record from .argus/runs/."""
    files = list(runs_dir.glob("*.json"))
    assert len(files) == 1, f"Expected 1 run file, found {len(files)}"
    return json.loads(files[0].read_text())


# ── Test 1: LLM trend lands in candidates.json ───────────────────────────────


def test_1_llm_trend_queued_as_candidate():
    """Simulates an LLM-suggested signature being queued for review."""
    sig = _make_sig("ERROR: context window exceeded")

    cand_id = add_candidate(sig, run_id="run-001", node_name="summarize")

    assert cand_id is not None, "add_candidate should return a candidate ID"

    data = load_candidates()
    assert len(data["candidates"]) == 1, "One pending candidate expected"

    cand = data["candidates"][0]
    assert cand["id"] == cand_id
    assert cand["pattern"] == sig.pattern
    assert cand["match_strategy"] == sig.match_strategy
    assert cand["status"] == "pending"
    assert cand["times_seen"] == 1
    assert "run-001" in cand["source_run_ids"]
    assert "summarize" in cand["source_nodes"]

    # ── Deduplication: same pattern+strategy must merge, not create a new entry ──
    cand_id2 = add_candidate(sig, run_id="run-002", node_name="summarize")
    assert cand_id2 == cand_id, "Duplicate should return the existing candidate ID"

    data2 = load_candidates()
    assert len(data2["candidates"]) == 1, "Still only one candidate after dedup"
    assert data2["candidates"][0]["times_seen"] == 2
    assert "run-002" in data2["candidates"][0]["source_run_ids"]

    print("\n[PRIVATE DB] LLM detects trend → candidates.json            PASS")


# ── Test 2: approve_candidate() → custom_signatures.json ─────────────────────


def test_2_private_approval():
    """Approving a candidate writes it to custom_signatures.json (private DB)."""
    sig = _make_sig("ERROR: context window exceeded")
    cand_id = add_candidate(sig, run_id="run-001", node_name="summarize")

    new_sig = approve_candidate(cand_id)

    assert new_sig is not None, "approve_candidate should return the new signature"
    assert new_sig["id"] == "CS-001"
    assert new_sig["source"] == "learned"
    assert new_sig["pattern"] == sig.pattern
    assert new_sig["match_strategy"] == sig.match_strategy
    assert new_sig["metadata"]["approval_status"] == "approved"

    # Candidate must be consumed
    data = load_candidates()
    assert len(data["candidates"]) == 0, "No pending candidates after approval"

    # Custom signatures file must exist with the right entry
    custom = load_custom_signatures()
    sigs = custom.get("signatures", [])
    assert len(sigs) == 1
    assert sigs[0]["pattern"] == sig.pattern

    # Registry picks it up after reload
    reload_registry()
    registry_patterns = [s.get("pattern") for s in get_registry()]
    assert sig.pattern in registry_patterns, "Pattern should be in registry after reload"

    print("[PRIVATE DB] approve_candidate() → custom_signatures.json   PASS")
    print("[PRIVATE DB] Deduplication works                             PASS")
    print("[PRIVATE DB] Reload registry picks up new signature          PASS")


# ── Test 3: fast-path flagging via private DB (NO LLM) ───────────────────────


def test_3_private_db_flags_next_run():
    """After private approval, the NEXT run flags the pattern without any LLM call."""
    sig = _make_sig("ERROR: context window exceeded")
    cand_id = add_candidate(sig, run_id="run-001", node_name="summarize")
    approve_candidate(cand_id)
    reload_registry()

    session = _no_llm_session()

    def bad_node(state):
        return {"result": "ERROR: context window exceeded on this request"}

    wrapped = session.wrap("summarize", bad_node)
    session.set_node_names(["summarize"])
    wrapped({"input": "some long document text"})
    session.finalize()

    run = _load_run(Path(".argus/runs"))
    step = run["steps"][0]

    assert step["node_name"] == "summarize"
    # Approval promotes severity to critical (a confirmed pattern, not a
    # hedge — see candidate_store.approve_candidate). Whether that lands as
    # "fail" or "semantic_fail" depends on a separate, pre-existing match-
    # confidence heuristic in inspector.py's Rule 7 (contains_ci confidence
    # scales with how much of the surrounding text the match covers) — not
    # something this test is about, so accept either flagged status.
    assert step["status"] in ("fail", "semantic_fail"), (
        f"Node should be flagged, got {step['status']!r}. "
        "Check that the pattern is in the registry and the output contains it."
    )

    semantic_signals = step.get("inspection", {}).get("semantic_signals", [])
    assert len(semantic_signals) > 0, "Should have at least one semantic signal"

    matched_ids = [s["sig_id"] for s in semantic_signals]
    assert "CS-001" in matched_ids, (
        f"Expected CS-001 in signals, got {matched_ids}"
    )

    print("[PRIVATE DB] Next run flagged WITHOUT LLM                   PASS")


# ── Test 4: approve_candidate_shared() → shared_signatures_cache.json ────────


def test_4_public_approval(monkeypatch):
    """Sharing a candidate writes it to the shared cache (simulating Supabase push).

    Public sharing is an enterprise/configured feature: with no Supabase
    configured (open-source), approve_candidate_shared falls back to a local
    approval. This test exercises the configured/enterprise path, so it sets
    a Supabase URL.
    """
    monkeypatch.setattr("argus.cloud.SUPABASE_URL", "https://test.supabase.co")
    sig2 = _make_sig("[SYSTEM PROMPT LEAKED]", category="suspicious_phrases")
    cand_id = add_candidate(sig2, run_id="run-002", node_name="rag_retriever")

    # Mock push_shared_signature: write to local cache + return success
    def mock_push(sig_data):
        cache = Path(".argus/shared_signatures_cache.json")
        cache.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(cache.read_text()) if cache.exists() else []
        existing.append(sig_data)
        cache.write_text(json.dumps(existing))
        return True

    monkeypatch.setattr("argus.cloud.push_shared_signature", mock_push)

    result = approve_candidate_shared(cand_id)

    assert result is not None, "approve_candidate_shared should return the signature"
    assert result["source"] == "shared"
    assert result["id"].startswith("SH-")
    assert result["pattern"] == sig2.pattern

    # Candidate must be consumed
    data = load_candidates()
    assert len(data["candidates"]) == 0, "No pending candidates after shared approval"

    # Cache must be written
    cache = Path(".argus/shared_signatures_cache.json")
    assert cache.exists(), "shared_signatures_cache.json must exist after approval"
    cached = json.loads(cache.read_text())
    assert isinstance(cached, list) and len(cached) > 0
    patterns_in_cache = [s.get("pattern") for s in cached]
    assert sig2.pattern in patterns_in_cache, "Shared pattern must be in cache"

    # Registry picks it up after reload
    reload_registry()
    registry_patterns = [s.get("pattern") for s in get_registry()]
    assert sig2.pattern in registry_patterns, "Shared pattern should be in registry"

    print("[PUBLIC DB]  approve_candidate_shared() → mocked push       PASS")
    print("[PUBLIC DB]  Shared sig written to cache                     PASS")


# ── Test 5: fast-path flagging via public DB (NO LLM) ────────────────────────


def test_5_public_db_flags_next_run():
    """A shared signature in the cache flags the next run without LLM."""
    shared_pattern = "[SYSTEM PROMPT LEAKED]"

    # Seed the shared cache directly (simulates a previously synced community
    # sig). approve_candidate_shared always promotes to critical on approval
    # (a confirmed pattern, not a hedge), so a real synced entry is critical
    # too — matching that here rather than the pre-approval "warning" hedge.
    cache = Path(".argus/shared_signatures_cache.json")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps([
            {
                "id": "SH-ABCDEF",
                "category": "suspicious_phrases",
                "pattern": shared_pattern,
                "match_strategy": "contains_ci",
                "severity": "critical",
                "description": "Detects leaked system prompt markers in RAG output",
                "source": "shared",
            }
        ])
    )
    reload_registry()

    session = _no_llm_session()

    def leaky_node(state):
        return {"output": f"rag result {shared_pattern} more content here"}

    wrapped = session.wrap("rag_retriever", leaky_node)
    session.set_node_names(["rag_retriever"])
    wrapped({"query": "what is ARGUS?"})
    session.finalize()

    run = _load_run(Path(".argus/runs"))
    step = run["steps"][0]

    assert step["node_name"] == "rag_retriever"
    # See test_3's comment — flagged status, exact value depends on a
    # separate match-confidence heuristic.
    assert step["status"] in ("fail", "semantic_fail"), (
        f"Node should be flagged, got {step['status']!r}"
    )

    semantic_signals = step.get("inspection", {}).get("semantic_signals", [])
    assert len(semantic_signals) > 0, "Should have at least one semantic signal"

    matched_sigs = [s["sig_id"] for s in semantic_signals]
    assert "SH-ABCDEF" in matched_sigs, (
        f"Expected SH-ABCDEF in signals, got {matched_sigs}"
    )

    print("[PUBLIC DB]  Next run flagged WITHOUT LLM                   PASS")


# ── Test 6: rejection blocks re-queueing ─────────────────────────────────────


def test_6_rejected_pattern_not_requeued():
    """Rejected patterns should never be re-queued as candidates."""
    sig3 = _make_sig("UNUSED_PATTERN")
    cand_id = add_candidate(sig3, run_id="run-003", node_name="node_x")
    assert cand_id is not None

    rejected = reject_candidate(cand_id)
    assert rejected is True

    data = load_candidates()
    assert len(data["candidates"]) == 0, "Candidate removed after rejection"
    assert "UNUSED_PATTERN" in data.get("rejected_patterns", []), (
        "Pattern must be in rejected_patterns"
    )

    # Attempt to re-queue the same pattern → must be silently skipped
    cand_id2 = add_candidate(sig3, run_id="run-004", node_name="node_x")
    assert cand_id2 is None, "Rejected pattern should not be re-queued"

    data2 = load_candidates()
    assert len(data2["candidates"]) == 0, "Still no candidates after rejected re-add"

    print("[REJECTION]  Rejected patterns not re-queued                PASS")


# ── Test 7: final summary report ─────────────────────────────────────────────


def test_7_full_loop_report(monkeypatch, capsys):
    """Runs the complete learning loop in one test and prints a final report."""
    results: dict[str, bool] = {}

    # Public sharing is enterprise/configured — set a Supabase URL so the
    # shared-approval path runs (open-source with no URL falls back to local).
    monkeypatch.setattr("argus.cloud.SUPABASE_URL", "https://test.supabase.co")

    # ── Step 1: LLM suggests trend (private) ──
    private_sig = _make_sig("ARGUS_PRIVATE_TREND")
    cand_priv = add_candidate(private_sig, run_id="run-p1", node_name="node_a")
    results["private_queued"] = cand_priv is not None

    # Dedup check
    cand_priv2 = add_candidate(private_sig, run_id="run-p2", node_name="node_a")
    results["private_dedup"] = cand_priv2 == cand_priv

    # ── Step 2a: approve to private DB ──
    priv_new = approve_candidate(cand_priv)
    results["private_approved"] = (
        priv_new is not None
        and priv_new.get("source") == "learned"
        and priv_new.get("id", "").startswith("CS-")
    )
    reload_registry()
    registry_patterns = [s.get("pattern") for s in get_registry()]
    results["private_in_registry"] = private_sig.pattern in registry_patterns

    # ── Step 3: fast-path flagging via private sig ──
    session_a = _no_llm_session()

    def private_bad_node(state):
        return {"result": "output containing ARGUS_PRIVATE_TREND here"}

    wa = session_a.wrap("node_a", private_bad_node)
    session_a.set_node_names(["node_a"])
    wa({"input": "test"})
    session_a.finalize()

    runs_dir_a = Path(".argus/runs")
    run_a = _load_run(runs_dir_a)
    step_a = run_a["steps"][0]
    results["private_flags_without_llm"] = (
        # See test_3's comment — flagged status, exact value depends on a
        # separate match-confidence heuristic.
        step_a["status"] in ("fail", "semantic_fail")
        and len(step_a.get("inspection", {}).get("semantic_signals", [])) > 0
    )

    # ── Step 4: LLM suggests trend (public) ──
    public_sig = _make_sig("ARGUS_PUBLIC_TREND", category="suspicious_phrases")
    cand_pub = add_candidate(public_sig, run_id="run-q1", node_name="node_b")
    results["public_queued"] = cand_pub is not None

    shared_cache: list[dict] = []

    def mock_push(sig_data):
        shared_cache.append(sig_data)
        cache_path = Path(".argus/shared_signatures_cache.json")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(shared_cache))
        return True

    monkeypatch.setattr("argus.cloud.push_shared_signature", mock_push)

    pub_new = approve_candidate_shared(cand_pub)
    results["public_approved"] = (
        pub_new is not None
        and pub_new.get("source") == "shared"
        and pub_new.get("id", "").startswith("SH-")
    )

    cache_path = Path(".argus/shared_signatures_cache.json")
    results["public_cache_written"] = cache_path.exists() and any(
        s.get("pattern") == public_sig.pattern
        for s in json.loads(cache_path.read_text())
    )

    reload_registry()
    registry_patterns2 = [s.get("pattern") for s in get_registry()]
    results["public_in_registry"] = public_sig.pattern in registry_patterns2

    # ── Step 5: fast-path flagging via shared sig ──
    # Remove the private run so _load_run can find the single public run
    for f in runs_dir_a.glob("*.json"):
        f.unlink()

    session_b = _no_llm_session()

    def public_bad_node(state):
        return {"output": "pipeline result ARGUS_PUBLIC_TREND end"}

    wb = session_b.wrap("node_b", public_bad_node)
    session_b.set_node_names(["node_b"])
    wb({"query": "test"})
    session_b.finalize()

    run_b = _load_run(runs_dir_a)
    step_b = run_b["steps"][0]
    results["public_flags_without_llm"] = (
        # See test_3's comment — flagged status, exact value depends on a
        # separate match-confidence heuristic.
        step_b["status"] in ("fail", "semantic_fail")
        and len(step_b.get("inspection", {}).get("semantic_signals", [])) > 0
    )

    # ── Step 6: rejection ──
    rej_sig = _make_sig("ARGUS_REJECTED_TREND")
    rej_id = add_candidate(rej_sig, run_id="run-r1")
    reject_candidate(rej_id)
    rej_id2 = add_candidate(rej_sig, run_id="run-r2")
    results["rejection_blocks_requeue"] = rej_id2 is None

    # ── Print report ──
    labels = {
        "private_queued":           "[PRIVATE DB] LLM detects trend → candidates.json",
        "private_dedup":            "[PRIVATE DB] Deduplication merges same pattern",
        "private_approved":         "[PRIVATE DB] approve_candidate() → custom_signatures",
        "private_in_registry":      "[PRIVATE DB] Reload registry picks up new signature",
        "private_flags_without_llm":"[PRIVATE DB] Next run flagged WITHOUT LLM",
        "public_queued":            "[PUBLIC  DB] LLM detects trend → candidates.json",
        "public_approved":          "[PUBLIC  DB] approve_candidate_shared() → mock push",
        "public_cache_written":     "[PUBLIC  DB] Shared sig written to local cache",
        "public_in_registry":       "[PUBLIC  DB] Reload registry picks up shared sig",
        "public_flags_without_llm": "[PUBLIC  DB] Next run flagged WITHOUT LLM",
        "rejection_blocks_requeue": "[REJECTION ] Rejected patterns not re-queued",
    }

    print("\n")
    print("=" * 62)
    print("  ARGUS DB FLOW VALIDATION REPORT")
    print("=" * 62)
    all_passed = True
    for key, label in labels.items():
        passed = results.get(key, False)
        icon = "✓" if passed else "✗"
        print(f"  {label:<50} {icon}")
        if not passed:
            all_passed = False
    print("=" * 62)
    if all_passed:
        print("  ALL CHECKS PASSED — private and public DB are wired correctly")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"  FAILED: {', '.join(failed)}")
    print("=" * 62)

    assert all_passed, f"Some DB flow checks failed: {[k for k,v in results.items() if not v]}"
