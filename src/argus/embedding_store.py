"""Embedding computation, caching, and similarity for semantic matching.

Uses OpenAI text-embedding-3-small to encode text as vectors and compute
cosine similarity for semantic pattern matching. Embeddings are cached
in SQLite to avoid recomputation and API costs.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_client: Any = None
_client_lock = threading.Lock()

_cache: EmbeddingCache | None = None
_cache_lock = threading.Lock()

_MODEL_NAME = "text-embedding-3-small"
_CACHE_DB_PATH = ".argus/embeddings_cache.db"
_EMBEDDING_DIM = 1536  # text-embedding-3-small output dimension


def _get_client() -> Any:
    """Lazy-load the OpenAI client (thread-safe)."""
    global _client  # noqa: PLW0603
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            from dotenv import load_dotenv  # noqa: PLC0415

            load_dotenv(override=True)
        except ImportError:
            pass
        from openai import OpenAI  # noqa: PLC0415

        from argus.user_config import resolve_openai_key  # noqa: PLC0415

        key = resolve_openai_key()
        _client = OpenAI(api_key=key) if key else OpenAI()
        return _client


def compute_embedding(text: str) -> list[float]:
    """Compute a single embedding vector via OpenAI API."""
    client = _get_client()
    resp = client.embeddings.create(input=[text], model=_MODEL_NAME)
    return resp.data[0].embedding


def compute_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Compute embeddings for a batch of texts via OpenAI API."""
    if not texts:
        return []
    client = _get_client()
    resp = client.embeddings.create(input=texts, model=_MODEL_NAME)
    # OpenAI may return embeddings out of order; sort by index
    sorted_data = sorted(resp.data, key=lambda d: d.index)
    return [d.embedding for d in sorted_data]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns float in [-1, 1]."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _text_hash(text: str) -> str:
    """SHA-256 hash of text for cache keying."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingCache:
    """SQLite-backed embedding cache at `.argus/embeddings_cache.db`."""

    def __init__(self, db_path: str | Path = _CACHE_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings "
            "(text_hash TEXT PRIMARY KEY, embedding TEXT, created_at TEXT)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def get(self, text: str) -> list[float] | None:
        """Retrieve cached embedding for text, or None if not cached."""
        h = _text_hash(text)
        with self._lock:
            row = self._conn.execute(
                "SELECT embedding FROM embeddings WHERE text_hash = ?", (h,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def put(self, text: str, embedding: list[float]) -> None:
        """Store an embedding in the cache."""
        h = _text_hash(text)
        blob = json.dumps(embedding)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO embeddings (text_hash, embedding, created_at) "
                "VALUES (?, ?, ?)",
                (h, blob, now),
            )
            self._conn.commit()

    def get_or_compute(self, text: str) -> list[float]:
        """Return cached embedding or compute, cache, and return it."""
        cached = self.get(text)
        if cached is not None:
            return cached
        vec = compute_embedding(text)
        self.put(text, vec)
        return vec

    def get_or_compute_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch version: only computes embeddings for cache misses."""
        results: list[list[float] | None] = [None] * len(texts)
        misses: list[tuple[int, str]] = []

        for i, text in enumerate(texts):
            cached = self.get(text)
            if cached is not None:
                results[i] = cached
            else:
                misses.append((i, text))

        if misses:
            miss_texts = [t for _, t in misses]
            miss_vecs = compute_embeddings_batch(miss_texts)
            for j, (idx, text) in enumerate(misses):
                self.put(text, miss_vecs[j])
                results[idx] = miss_vecs[j]

        return results  # type: ignore[return-value]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


def _get_cache() -> EmbeddingCache:
    """Return the module-level singleton cache instance."""
    global _cache  # noqa: PLW0603
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        _cache = EmbeddingCache()
        return _cache


def get_cached_embedding(text: str) -> list[float]:
    """Public helper: get or compute an embedding with caching."""
    return _get_cache().get_or_compute(text)


def get_cached_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Public helper: batch get or compute embeddings with caching."""
    return _get_cache().get_or_compute_batch(texts)
