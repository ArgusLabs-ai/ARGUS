"""Tests for candidate_store (B-3: regex compile-validation on add, F-16)."""

from __future__ import annotations

import pytest

from argus.candidate_store import add_candidate, load_candidates
from argus.models import SuggestedSignature


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Run every test in a temp directory."""
    monkeypatch.chdir(tmp_path)


def _sig(pattern: str, strategy: str = "regex") -> SuggestedSignature:
    return SuggestedSignature(
        pattern=pattern,
        match_strategy=strategy,
        proposed_category="placeholder_outputs",
        severity="warning",
        description="test candidate",
        evidence=("sample",),
        confidence=0.8,
        reasoning="test",
    )


def test_add_candidate_rejects_invalid_regex() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        add_candidate(_sig("[unclosed"), run_id="run-1", node_name="n1")
    # Rejected before any persistence: nothing queued.
    assert load_candidates()["candidates"] == []


def test_add_candidate_valid_regex_round_trips() -> None:
    cand_id = add_candidate(_sig(r"placeholder_\w+"), run_id="run-1", node_name="n1")
    assert cand_id is not None
    pending = load_candidates()["candidates"]
    assert len(pending) == 1
    assert pending[0]["pattern"] == r"placeholder_\w+"


def test_non_regex_strategy_not_compile_validated() -> None:
    # contains_ci treats the pattern as a literal — '[unclosed' is a valid literal.
    cand_id = add_candidate(_sig("[unclosed", strategy="contains_ci"), run_id="run-1")
    assert cand_id is not None
