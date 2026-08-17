"""Tests for auto-generalization of LLM-suggested signatures."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from argus.models import SuggestedSignature
from argus.signature_generalizer import (
    _heuristic_generalize,
    _llm_generalize,
    cluster_with_existing,
    generalize_signature,
)

# ── Helper ───────────────────────────────────────────────────────────────────


def _make_sig(
    pattern: str = "I cannot provide financial advice",
    strategy: str = "contains_ci",
    evidence: tuple[str, ...] = ("I cannot provide financial advice",),
) -> SuggestedSignature:
    return SuggestedSignature(
        pattern=pattern,
        match_strategy=strategy,
        proposed_category="semantic_refusal",
        severity="warning",
        description="test pattern",
        evidence=evidence,
        confidence=0.85,
        reasoning="test",
    )


# ── Heuristic generalization tests ──────────────────────────────────────────


@pytest.mark.unit
def test_heuristic_generalize_produces_regex():
    """A contains_ci pattern with enough content words becomes a regex."""
    result = _heuristic_generalize("I cannot provide financial advice")
    assert result is not None
    compiled = re.compile(result, re.IGNORECASE)
    # Should match the original
    assert compiled.search("I cannot provide financial advice")
    # Should match variants via synonym groups
    assert compiled.search("unable to offer investment guidance")
    assert compiled.search("can't give monetary recommendations")


@pytest.mark.unit
def test_heuristic_short_pattern_returns_none():
    """Patterns with fewer than 3 content words return None."""
    assert _heuristic_generalize("error occurred") is None
    assert _heuristic_generalize("no") is None


@pytest.mark.unit
def test_heuristic_preserves_unknown_words():
    """Words not in the synonym map are regex-escaped and kept."""
    result = _heuristic_generalize(
        "the system encountered a catastrophic failure"
    )
    assert result is not None
    compiled = re.compile(result, re.IGNORECASE)
    assert compiled.search("system encountered a catastrophic failure")


# ── generalize_signature tests ──────────────────────────────────────────────


@pytest.mark.unit
def test_generalize_signature_converts_to_regex():
    """generalize_signature converts contains_ci to regex."""
    sig = _make_sig()
    # Patch _llm_generalize to return None so heuristic is used
    with patch(
        "argus.signature_generalizer._llm_generalize", return_value=None
    ):
        result = generalize_signature(sig)
    assert result.match_strategy == "regex"
    assert result.generalized is True
    assert result.original_pattern == "I cannot provide financial advice"
    # The pattern should be a valid regex
    re.compile(result.pattern, re.IGNORECASE)


@pytest.mark.unit
def test_generalize_preserves_regex_strategy():
    """Signatures already using regex strategy are returned unchanged."""
    sig = _make_sig(
        pattern=r"(?:error|failure)\s+detected",
        strategy="regex",
    )
    result = generalize_signature(sig)
    assert result is sig  # same object, unchanged


@pytest.mark.unit
def test_generalize_preserves_original_pattern():
    """original_pattern field stores the pre-generalization literal."""
    sig = _make_sig()
    with patch(
        "argus.signature_generalizer._llm_generalize", return_value=None
    ):
        result = generalize_signature(sig)
    assert result.original_pattern == "I cannot provide financial advice"
    assert result.generalized is True


@pytest.mark.unit
def test_generalize_short_pattern_unchanged():
    """Short patterns are returned unchanged."""
    sig = _make_sig(pattern="error", evidence=("error",))
    with patch(
        "argus.signature_generalizer._llm_generalize", return_value=None
    ):
        result = generalize_signature(sig)
    assert result.match_strategy == "contains_ci"
    assert result.generalized is False


# ── LLM generalization fallback tests ───────────────────────────────────────


@pytest.mark.unit
def test_llm_failure_falls_back_to_heuristic():
    """When LLM generalization fails, heuristic is used."""
    sig = _make_sig()
    with patch(
        "argus.signature_generalizer._llm_generalize", return_value=None
    ):
        result = generalize_signature(sig)
    # Should still produce a regex via heuristic
    assert result.match_strategy == "regex"
    assert result.generalized is True


@pytest.mark.unit
def test_no_api_key_graceful_fallback():
    """Missing API key doesn't crash — falls back to heuristic."""
    sig = _make_sig()
    with patch(
        "argus.signature_generalizer._llm_generalize",
        return_value=None,
    ):
        result = generalize_signature(sig)
    # Heuristic should still produce a regex
    assert result.match_strategy == "regex"
    assert result.generalized is True


# ── LLM transport tests ─────────────────────────────────────────────────────


def _mock_llm(monkeypatch, content, available=True):
    """Point _llm_generalize at a fake shared LLM path, return captured kwargs."""
    captured: dict[str, object] = {}

    def _fake(**kw):
        captured.update(kw)
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: available)
    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _fake)
    return captured


@pytest.mark.unit
def test_llm_generalize_uses_shared_llm_path(monkeypatch):
    """The regex comes back off llm_proxy, not a module-local OpenAI client."""
    _mock_llm(monkeypatch, r"(?:cannot|can't)\s+(?:provide|give)\s+\S+\s+advice")
    result = _llm_generalize(
        "I cannot provide financial advice",
        ("I cannot provide financial advice",),
    )
    assert result == r"(?:cannot|can't)\s+(?:provide|give)\s+\S+\s+advice"


@pytest.mark.unit
def test_llm_generalize_never_touches_embedding_store_client(monkeypatch):
    """Generalization no longer needs the caller's own OPENAI_API_KEY.

    A user on Anthropic or Gemini has no OpenAI client to build; before this
    went through llm_proxy they got no LLM generalization at all.
    """

    def _boom():
        raise AssertionError("_get_client must not be called")

    monkeypatch.setattr("argus.embedding_store._get_client", _boom)
    _mock_llm(monkeypatch, r"unable\s+to\s+\S+\s+guidance")
    result = _llm_generalize(
        "unable to offer guidance",
        ("unable to offer guidance",),
    )
    assert result == r"unable\s+to\s+\S+\s+guidance"


@pytest.mark.unit
def test_llm_generalize_asks_for_the_cheap_tier_with_a_bounded_timeout(monkeypatch):
    """The call is bounded and tier-hinted so resolve_model() can remap it."""
    captured = _mock_llm(monkeypatch, r"a\s+b\s+c")
    _llm_generalize("a b c", ("a b c",))
    assert captured["model"] == "gpt-4o-mini"
    assert captured["timeout"] == 15.0
    assert captured["max_tokens"] == 300
    assert captured["temperature"] == 0.0
    # Free-text regex, not JSON — no response_format is imposed.
    assert "response_format" not in captured


@pytest.mark.unit
def test_llm_generalize_returns_none_when_no_llm_configured(monkeypatch):
    """No key and not logged in — skip the call entirely, caller falls back."""
    _mock_llm(monkeypatch, "irrelevant", available=False)
    assert _llm_generalize("a b c", ("a b c",)) is None


@pytest.mark.unit
def test_llm_generalize_returns_none_on_transport_error(monkeypatch):
    """llm_proxy reports failure in-band as {"error": ...}, not by raising."""
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    monkeypatch.setattr(
        "argus.llm_proxy.create_chat_completion",
        lambda **kw: {"error": "Daily limit reached (200 calls/day)."},
    )
    assert _llm_generalize("a b c", ("a b c",)) is None


@pytest.mark.unit
def test_llm_generalize_returns_none_on_exception(monkeypatch):
    """An unexpected raise still fails open."""
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)

    def _raise(**kw):
        raise ConnectionError("timeout")

    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", _raise)
    assert _llm_generalize("a b c", ("a b c",)) is None


@pytest.mark.unit
def test_llm_generalize_strips_code_fences(monkeypatch):
    """Models that wrap the regex in a fenced block still produce a pattern."""
    _mock_llm(monkeypatch, "```python\ncannot\\s+provide\n```")
    result = _llm_generalize("cannot provide", ("I cannot provide",))
    assert result == r"cannot\s+provide"


@pytest.mark.unit
def test_llm_generalize_rejects_pattern_that_misses_evidence(monkeypatch):
    """A regex too narrow for its own evidence is discarded."""
    _mock_llm(monkeypatch, r"completely\s+unrelated")
    assert _llm_generalize("a b c", ("I cannot provide advice",)) is None


@pytest.mark.unit
def test_llm_generalize_rejects_empty_response(monkeypatch):
    """An empty pattern compiles and matches everything — reject it."""
    _mock_llm(monkeypatch, "   ")
    assert _llm_generalize("a b c", ("a b c",)) is None


@pytest.mark.unit
def test_llm_generalize_rejects_missing_choices(monkeypatch):
    """A well-formed-but-empty response body is a failure, not a pattern."""
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", lambda **kw: {})
    assert _llm_generalize("a b c", ("a b c",)) is None


@pytest.mark.unit
def test_generalize_signature_prefers_the_llm_regex(monkeypatch):
    """End to end: an LLM pattern wins over the heuristic one."""
    _mock_llm(monkeypatch, r"(?:cannot|unable\s+to)\s+\S+\s+financial\s+advice")
    result = generalize_signature(_make_sig())
    assert result.match_strategy == "regex"
    assert result.generalized is True
    assert result.pattern == r"(?:cannot|unable\s+to)\s+\S+\s+financial\s+advice"
    assert result.original_pattern == "I cannot provide financial advice"


# ── Clustering tests ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cluster_empty_candidates_returns_none():
    """No candidates means no cluster match."""
    sig = _make_sig()
    assert cluster_with_existing(sig, []) is None


@pytest.mark.unit
def test_cluster_merges_similar():
    """Semantically similar candidates are clustered."""
    sig = _make_sig(pattern="I cannot help with that")

    candidates = [
        {
            "id": "cand-abc123",
            "pattern": "I am unable to assist with that",
            "status": "pending",
        }
    ]

    # Mock embeddings: very similar vectors (cosine > 0.85)
    mock_emb_a = [1.0, 0.0, 0.0]
    mock_emb_b = [0.98, 0.2, 0.0]

    call_count = 0

    def mock_get_embedding(text: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_emb_a
        return mock_emb_b

    with patch(
        "argus.embedding_store.get_cached_embedding",
        side_effect=mock_get_embedding,
    ):
        result = cluster_with_existing(sig, candidates)

    assert result == "cand-abc123"


@pytest.mark.unit
def test_cluster_no_match_below_threshold():
    """Dissimilar candidates are not clustered."""
    sig = _make_sig(pattern="I cannot help with that")

    candidates = [
        {
            "id": "cand-xyz789",
            "pattern": "The weather is sunny today",
            "status": "pending",
        }
    ]

    # Mock embeddings: orthogonal vectors (cosine = 0)
    mock_emb_a = [1.0, 0.0, 0.0]
    mock_emb_b = [0.0, 1.0, 0.0]

    call_count = 0

    def mock_get_embedding(text: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_emb_a
        return mock_emb_b

    with patch(
        "argus.embedding_store.get_cached_embedding",
        side_effect=mock_get_embedding,
    ):
        result = cluster_with_existing(sig, candidates)

    assert result is None


@pytest.mark.unit
def test_cluster_skips_non_pending():
    """Only pending candidates are considered for clustering."""
    sig = _make_sig(pattern="I cannot help")

    candidates = [
        {
            "id": "cand-old",
            "pattern": "I cannot help",
            "status": "approved",  # not pending
        }
    ]

    with patch(
        "argus.embedding_store.get_cached_embedding",
        return_value=[1.0, 0.0, 0.0],
    ):
        result = cluster_with_existing(sig, candidates)

    assert result is None


@pytest.mark.unit
def test_cluster_no_api_key_returns_none():
    """Missing API key during clustering returns None gracefully."""
    sig = _make_sig()
    candidates = [
        {"id": "cand-1", "pattern": "test", "status": "pending"}
    ]

    with patch(
        "argus.embedding_store.get_cached_embedding",
        side_effect=Exception("No API key"),
    ):
        result = cluster_with_existing(sig, candidates)

    assert result is None
