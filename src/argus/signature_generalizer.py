"""Auto-generalize LLM-suggested signatures before storing.

Converts literal contains_ci/exact_ci patterns into regex patterns
that cover common semantic rephrasings. Also clusters semantically
similar candidates to avoid duplicates.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from argus.models import SuggestedSignature

# ── Synonym map for heuristic generalization ─────────────────────────────────

_SYNONYM_MAP: dict[str, str] = {
    "cannot": r"(?:cannot|can't|can\s+not|unable\s+to)",
    "can't": r"(?:cannot|can't|can\s+not|unable\s+to)",
    "unable": r"(?:unable|cannot|can't)",
    "provide": r"(?:provide|give|offer|supply|share)",
    "assist": r"(?:assist|help|aid|support)",
    "help": r"(?:help|assist|aid|support)",
    "sorry": r"(?:sorry|apologize|apologies)",
    "apologize": r"(?:sorry|apologize|apologies)",
    "information": r"(?:information|info|data|details)",
    "advice": r"(?:advice|guidance|recommendations|counsel)",
    "financial": r"(?:financial|investment|monetary|fiscal)",
    "medical": r"(?:medical|health|clinical|healthcare)",
    "legal": r"(?:legal|law|juridical|attorney)",
    "request": r"(?:request|query|question|ask)",
    "access": r"(?:access|reach|connect\s+to|retrieve)",
    "real-time": r"(?:real-time|realtime|live|current|up-to-date)",
    "training": r"(?:training|knowledge|learned)",
    "context": r"(?:context|information|details|input)",
    "accurate": r"(?:accurate|precise|correct|reliable)",
    "answer": r"(?:answer|response|reply|output)",
    "generate": r"(?:generate|create|produce|make)",
    "specific": r"(?:specific|particular|exact|precise)",
    "enough": r"(?:enough|sufficient|adequate)",
    "general": r"(?:general|generic|broad|overall)",
    "purposes": r"(?:purposes|use|reference|informational\s+use)",
    "beyond": r"(?:beyond|past|after|outside)",
    "based": r"(?:based|according|per|given)",
}

_STOP_WORDS = frozenset(
    {
        "i",
        "i'm",
        "im",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "it",
        "its",
        "this",
        "that",
        "as",
        "if",
        "not",
        "no",
        "so",
        "up",
        "out",
        "about",
        "into",
        "than",
        "also",
        "don't",
        "doesn't",
        "didn't",
    }
)

_LLM_GENERALIZE_PROMPT = """\
You are a regex pattern expert. Given a literal failure pattern and \
evidence examples from LLM outputs, produce a single Python regex \
(compatible with re.IGNORECASE) that matches the original and all \
semantic rephrasings.

Rules:
- Use (?:alt1|alt2) for synonym groups
- Use \\s+ for flexible whitespace
- Keep it tight — avoid .* wildcards that match everything
- The regex must match ALL the evidence examples provided
- Return ONLY the regex string, no explanation, no code fences

Pattern: {pattern}
Evidence: {evidence}

Regex:"""


# ── Heuristic generalization ─────────────────────────────────────────────────


def _heuristic_generalize(pattern: str) -> str | None:
    """Generate a regex from a literal pattern using synonym mapping.

    Returns None if the pattern has fewer than 3 content words
    (not enough signal to generalize safely).
    """
    tokens = pattern.lower().split()
    content_tokens = [t for t in tokens if t not in _STOP_WORDS]

    if len(content_tokens) < 3:
        return None

    regex_parts: list[str] = []
    for token in tokens:
        clean = token.strip(".,;:!?\"'")
        lower = clean.lower()
        if lower in _STOP_WORDS:
            continue  # skip stop words in the regex
        if lower in _SYNONYM_MAP:
            regex_parts.append(_SYNONYM_MAP[lower])
        else:
            regex_parts.append(re.escape(clean))

    if len(regex_parts) < 2:
        return None

    # Allow optional filler words (stop words) between content tokens
    joiner = r"(?:\s+\S+)*?\s+"
    regex = joiner.join(regex_parts)

    # Validate it compiles
    try:
        re.compile(regex, re.IGNORECASE)
    except re.error:
        return None

    return regex


# ── LLM generalization ──────────────────────────────────────────────────────

# Cheap tier. providers.resolve_model() maps this to the active provider's
# equivalent (claude-3-5-haiku / gemini-2.5-flash) for non-OpenAI users.
_LLM_MODEL = "gpt-4o-mini"
_LLM_MAX_TOKENS = 300
# Runs once per suggested signature in a loop at session end, so it needs a
# bound. Not one of the per-node hot-path budgets; sized for a ≤300-token
# completion with headroom.
_LLM_TIMEOUT_S = 15.0


def _llm_generalize(
    pattern: str,
    evidence: tuple[str, ...],
) -> str | None:
    """Ask the cheap-tier model to produce a generalized regex.

    Routed through llm_proxy, so this resolves the same way every other LLM
    call in argus does: the active provider's own key (OpenAI, Anthropic, or
    Google) if there is one, else the hosted proxy for logged-in users.

    Returns None on any failure (no LLM configured, network, bad regex) and
    the caller falls back to the heuristic.
    """
    # Load .env if present (no-op if python-dotenv not installed)
    try:
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(override=True)
    except ImportError:
        pass

    from argus.llm_proxy import create_chat_completion, is_available  # noqa: PLC0415

    if not is_available():
        return None

    prompt = _LLM_GENERALIZE_PROMPT.format(
        pattern=pattern,
        evidence=json.dumps(list(evidence)),
    )

    try:
        result = create_chat_completion(
            model=_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=_LLM_MAX_TOKENS,
            timeout=_LLM_TIMEOUT_S,
        )
        if "error" in result:
            return None

        choices = result.get("choices", [])
        if not choices:
            return None
        regex = (choices[0]["message"]["content"] or "").strip()
        # Strip code fences if the LLM wraps it
        regex = regex.strip("`").strip()
        if regex.startswith("python"):
            regex = regex[6:].strip()
        if not regex:
            return None  # empty pattern would match every output

        # Validate it compiles
        compiled = re.compile(regex, re.IGNORECASE)

        # Validate it matches all evidence strings
        for ev in evidence:
            if not compiled.search(ev):
                return None  # too narrow — fall back to heuristic

        return regex
    except Exception:
        return None


# ── Main entry point ─────────────────────────────────────────────────────────


def generalize_signature(
    sig: SuggestedSignature,
) -> SuggestedSignature:
    """Auto-generalize a literal pattern into a regex.

    Only acts on contains_ci / exact_ci strategies with 3+ content words.
    Returns the signature unchanged if generalization fails or doesn't apply.
    """
    if sig.match_strategy not in ("contains_ci", "exact_ci"):
        return sig

    # Try LLM first (produces tighter regex), fall back to heuristic
    regex = _llm_generalize(sig.pattern, sig.evidence)
    if regex is None:
        regex = _heuristic_generalize(sig.pattern)

    if regex is None:
        return sig

    return replace(
        sig,
        pattern=regex,
        match_strategy="regex",
        original_pattern=sig.pattern,
        generalized=True,
    )


# ── Candidate clustering ────────────────────────────────────────────────────

_CLUSTER_THRESHOLD = 0.85


def cluster_with_existing(
    sig: SuggestedSignature,
    candidates: list[dict[str, Any]],
) -> str | None:
    """Check if a semantically similar candidate already exists.

    Returns the candidate ID if cosine similarity >= 0.85, else None.
    Skips silently without API key.
    """
    if not candidates:
        return None

    # Use the original pattern for embedding (pre-generalization literal)
    text = sig.original_pattern or sig.pattern

    try:
        from argus import embedding_store  # noqa: PLC0415

        sig_emb = embedding_store.get_cached_embedding(text)
    except Exception:
        return None

    best_id: str | None = None
    best_score = 0.0

    for cand in candidates:
        if cand.get("status") != "pending":
            continue
        cand_pattern = cand.get("original_pattern") or cand.get("pattern", "")
        if not cand_pattern:
            continue
        try:
            cand_emb = embedding_store.get_cached_embedding(cand_pattern)
            score = embedding_store.cosine_similarity(sig_emb, cand_emb)
            if score >= _CLUSTER_THRESHOLD and score > best_score:
                best_score = score
                best_id = cand["id"]
        except Exception:
            continue

    return best_id
