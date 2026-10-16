"""Judge verdict cache: the store, and the two judges that use it.

The cache serves a verdict only to a request that can claim to be reproducible
(`argus.determinism.gate`: temperature 0.0, a seed, a route that honors it), so every
judge call in this file is seeded and routed through a provider that takes a seed.
The LLM transport is faked throughout (no test reaches a network). The cache is off
by default in the suite (tests/conftest.py); the autouse fixture below turns it on for
this file, with the install key under tmp_path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import stat
import sys
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest
from test_determinism import _PURPOSES, _graph, _ReviewWire, _Wire

from argus import ArgusRecorder, providers, user_config, verdict_cache
from argus.ledger import LedgerRow
from argus.llm_proxy import judge_route
from argus.models import ArgusConfig, SemanticSignal, ValidatorResult
from argus.review import Reviewer
from argus.semantic_checker import check_semantic_coherence as _check_semantic_coherence
from argus.storage import load_run

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")

# The cache serves only a request that can claim to be reproducible: a seed, on a route that
# honors it. These tests are about the cache, so every coherence call here is seeded (a test
# may pass its own `seed=`); tests/test_determinism.py covers the gate itself.
check_semantic_coherence = partial(_check_semantic_coherence, seed=0)


@pytest.fixture(autouse=True)
def _cache_on(tmp_argus_dir, monkeypatch):
    monkeypatch.delenv("ARGUS_VERDICT_CACHE", raising=False)
    monkeypatch.delenv("ARGUS_RUN_SALT", raising=False)
    monkeypatch.setenv("ARGUS_JUDGE_SEED", "0")  # sessions and recorders seed their judges
    monkeypatch.setattr(user_config, "resolve_key", lambda provider: "k")
    monkeypatch.setattr(user_config, "get_provider", lambda: "openai")  # a seeded route
    monkeypatch.setattr(user_config, "get_model_overrides", lambda: {})
    monkeypatch.setattr(verdict_cache, "_warned", set())
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)


@pytest.fixture
def scope(tmp_path):
    found = verdict_cache.current_scope()
    assert found is not None
    return found


# ── a fake LLM transport ─────────────────────────────────────────────────────

_VERDICT = {
    "pass": True,
    "reason": "fine",
    "confidence": 0.9,
    "failure_kind": "other",
    "evidence_considered": [],
    "overridden_signals": [],
    "disambiguation_verdicts": [],
}


def _tool_reply(arguments, name="record_verdict"):
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _content_reply(text):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _like_an_adapter(reply, kwargs):
    """A seeded request comes back marked ``seed_honored``, as the real adapters mark it."""
    return {**reply, "seed_honored": True} if kwargs.get("seed") is not None else reply


class _Llm:
    """Records every request; answers with the scripted replies (the last one repeats)."""

    def __init__(self, monkeypatch, *replies):
        self.calls: list[dict] = []
        self._replies = list(replies) or [_tool_reply(_VERDICT)]
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", self)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._replies[min(len(self.calls), len(self._replies)) - 1]
        return _like_an_adapter(reply(kwargs) if callable(reply) else reply, kwargs)


def _judge(scope, llm_scope=True, **kwargs):
    kwargs.setdefault("model", "gpt-4o-mini")
    return check_semantic_coherence(
        "n1", {"q": "hello"}, {"a": "world"}, cache=scope if llm_scope else None, **kwargs
    )


def _rows(scope):
    with closing(sqlite3.connect(scope.db_path)) as conn:
        return conn.execute(
            "SELECT content_hash, model, key_salt, verdict, mac FROM verdicts"
        ).fetchall()


def _key(**overrides):
    request = {"model": "m", "messages": [{"role": "user", "content": "x"}], "temperature": 0.0}
    request.update(overrides)
    return verdict_cache.request_key(request)


# ═════════════════════════════════════════════════════════════════════════════
# The store
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestStore:
    def test_roundtrip_is_exact(self, scope):
        verdict = {"pass": False, "reason": "unicode ✓ ünï", "n": [1, 2.5, None], "z": {"a": 1}}
        assert verdict_cache.store(scope, _key(), "m", verdict) is True
        assert verdict_cache.lookup(scope, _key(), "m") == verdict

    def test_missing_row_and_missing_database_are_misses_and_create_nothing(self, tmp_path, scope):
        elsewhere = verdict_cache.CacheScope(tmp_path / "other" / "v.db", "s")
        verdict_cache.store(elsewhere, _key(), "m", {"v": 1})  # so an install key exists
        assert verdict_cache.install_key_path().exists()
        assert verdict_cache.lookup(scope, _key(), "m") is None
        assert not scope.db_path.exists()
        verdict_cache.store(scope, _key(), "m", {"pass": True})
        assert verdict_cache.lookup(scope, _key(max_tokens=5), "m") is None

    def test_model_and_salt_are_part_of_the_key(self, scope):
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        assert verdict_cache.lookup(scope, _key(), "other-model") is None
        other = verdict_cache.CacheScope(scope.db_path, "another-salt")
        assert verdict_cache.lookup(other, _key(), "m") is None

    def test_another_salt_never_overwrites_a_row(self, scope):
        a = verdict_cache.CacheScope(scope.db_path, "salt-a")
        b = verdict_cache.CacheScope(scope.db_path, "salt-b")
        verdict_cache.store(a, _key(), "m", {"v": "A"})
        verdict_cache.store(b, _key(), "m", {"v": "B"})
        assert verdict_cache.lookup(a, _key(), "m") == {"v": "A"}
        assert verdict_cache.lookup(b, _key(), "m") == {"v": "B"}
        assert len(_rows(a)) == 2

    def test_same_key_stored_again_replaces_its_own_row(self, scope):
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        verdict_cache.store(scope, _key(), "m", {"v": 2})
        assert verdict_cache.lookup(scope, _key(), "m") == {"v": 2}
        assert len(_rows(scope)) == 1

    def test_a_row_is_authenticated_not_secret(self, scope):
        """The cache guards forgery: the verdict is readable (as the run files beside it
        are), the install key is not on disk next to it."""
        verdict_cache.store(scope, _key(), "m", {"pass": True, "reason": "plain-reason"})
        ((_, _, _, verdict_json, mac),) = _rows(scope)
        assert "plain-reason" in verdict_json
        assert len(mac) == 64 and int(mac, 16) >= 0
        install_key = verdict_cache.install_key_path().read_bytes()
        assert install_key not in b"".join(
            p.read_bytes() for p in scope.db_path.parent.iterdir() if p.is_file()
        )

    def test_a_tampered_verdict_is_a_miss(self, scope):
        verdict_cache.store(scope, _key(), "m", {"pass": True, "reason": "real"})
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute(
                "UPDATE verdicts SET verdict = ?", ('{"pass": false, "reason": "forged"}',)
            )
        assert verdict_cache.lookup(scope, _key(), "m") is None

    def test_a_tampered_mac_is_a_miss(self, scope):
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute("UPDATE verdicts SET mac = ?", ("0" * 64,))
        assert verdict_cache.lookup(scope, _key(), "m") is None

    def test_a_row_moved_to_another_model_or_salt_is_a_miss(self, scope):
        """The row context is part of the MAC: a verdict cannot be re-labelled."""
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        (ch, _, salt, verdict_json, mac) = _rows(scope)[0]
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            for model, key_salt in (("m2", salt), ("m", "salt-2")):
                conn.execute(
                    "INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, 'x')",
                    (ch, model, key_salt, verdict_json, mac),
                )
        assert verdict_cache.lookup(scope, _key(), "m2") is None
        assert (
            verdict_cache.lookup(verdict_cache.CacheScope(scope.db_path, "salt-2"), _key(), "m")
            is None
        )
        assert verdict_cache.lookup(scope, _key(), "m") == {"v": 1}

    def test_a_row_cannot_be_forged_from_the_database_file_alone(self, scope):
        """Everything the file shows (hash, model, salt, the verdict) is not enough to
        make a row the cache accepts: the MAC needs the install key."""
        ch = _key()
        verdict_cache.store(scope, ch, "m", {"pass": True, "reason": "real"})
        (_, _, salt, _, _) = _rows(scope)[0]
        context = verdict_cache._row_context(ch, "m", salt)
        forged = json.dumps({"pass": False, "reason": "forged"}, sort_keys=True)
        for guess in (b"", bytes(32), hashlib.sha256(f"{ch}{salt}".encode()).digest()):
            mac = hmac.new(guess, context + b"\x00" + forged.encode(), hashlib.sha256).hexdigest()
            with closing(sqlite3.connect(scope.db_path)) as conn, conn:
                conn.execute("UPDATE verdicts SET verdict = ?, mac = ?", (forged, mac))
            assert verdict_cache.lookup(scope, ch, "m") is None

    def test_a_different_install_key_is_a_miss(self, scope):
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        verdict_cache.install_key_path().write_bytes(bytes(32))
        assert verdict_cache.lookup(scope, _key(), "m") is None
        # a store under the new key works, and replaces the unreadable row
        assert verdict_cache.store(scope, _key(), "m", {"v": 2}) is True
        assert verdict_cache.lookup(scope, _key(), "m") == {"v": 2}

    @pytest.mark.parametrize("payload", ["[1, 2]", "7", '"text"', "null"])
    def test_a_non_object_payload_is_a_miss(self, scope, payload):
        ch = _key()
        verdict_cache.store(scope, ch, "m", {"v": 1})
        (_, _, salt, _, _) = _rows(scope)[0]
        context = verdict_cache._row_context(ch, "m", salt)
        mac = verdict_cache._mac(verdict_cache.install_key_path().read_bytes(), context, payload)
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute("UPDATE verdicts SET verdict = ?, mac = ?", (payload, mac))
        assert verdict_cache.lookup(scope, ch, "m") is None

    @pytest.mark.parametrize("column", ["verdict", "mac"])
    @pytest.mark.parametrize("value", [7, b"bytes", "\u00e9"])
    def test_a_row_with_the_wrong_kind_of_value_is_a_miss_not_a_crash(self, scope, column, value):
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute(f"UPDATE verdicts SET {column} = ?", (value,))
        assert verdict_cache.lookup(scope, _key(), "m") is None

    @posix_only
    def test_files_and_directories_are_private(self, tmp_path):
        fresh = verdict_cache.CacheScope(tmp_path / "new-project" / ".argus" / "v.db", "s")
        assert verdict_cache.store(fresh, _key(), "m", {"v": 1}) is True
        mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
        assert mode(fresh.db_path) == 0o600
        assert mode(fresh.db_path.parent) == 0o700
        key_file = verdict_cache.install_key_path()
        assert mode(key_file) == 0o600
        assert mode(key_file.parent) == 0o700
        assert len(key_file.read_bytes()) == 32
        # SQLite's own side files inherit the database's mode
        for side in fresh.db_path.parent.iterdir():
            assert mode(side) & 0o077 == 0, side

    def test_the_install_key_lives_next_to_the_user_config_not_in_the_project(
        self, monkeypatch, tmp_path, scope
    ):
        monkeypatch.delenv("ARGUS_VERDICT_KEY_FILE")
        monkeypatch.setattr(user_config, "_CONFIG_DIR", tmp_path / "home" / ".argus")
        path = verdict_cache.install_key_path()
        assert path == tmp_path / "home" / ".argus" / "verdict_cache.key"
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        assert path.is_file()
        assert scope.db_path.parent not in path.parents

    def test_concurrent_first_use_ends_with_one_key_every_writer_can_read(self, tmp_path, scope):
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def work(i):
            try:
                barrier.wait(10)
                assert verdict_cache.store(scope, _key(max_tokens=i), "m", {"v": i})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors
        for i in range(8):
            assert verdict_cache.lookup(scope, _key(max_tokens=i), "m") == {"v": i}
        assert [p.name for p in verdict_cache.install_key_path().parent.iterdir()] == [
            "verdict_cache.key"
        ]

    def test_rows_are_pruned_by_count_and_by_age(self, scope, monkeypatch):
        monkeypatch.setattr(verdict_cache, "MAX_ROWS", 3)
        for i in range(5):
            verdict_cache.store(scope, _key(max_tokens=i), "m", {"v": i})
        assert len(_rows(scope)) == 3
        assert verdict_cache.lookup(scope, _key(max_tokens=0), "m") is None  # the oldest went
        assert verdict_cache.lookup(scope, _key(max_tokens=4), "m") == {"v": 4}

        monkeypatch.setattr(verdict_cache, "MAX_ROWS", 100)
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute("UPDATE verdicts SET created_at = '2000-01-01T00:00:00.000000+00:00'")
        verdict_cache.store(scope, _key(max_tokens=99), "m", {"v": 99})
        assert len(_rows(scope)) == 1  # only the fresh row survives the age bound

    def test_a_row_older_than_the_age_limit_is_a_miss_with_no_store_to_prune_it(self, scope):
        verdict_cache.store(scope, _key(max_tokens=1), "m", {"v": 1})
        verdict_cache.store(scope, _key(max_tokens=2), "m", {"v": 2})
        verdict_cache.store(scope, _key(max_tokens=3), "m", {"v": 3})
        ages = {1: 200, 2: verdict_cache.MAX_AGE_DAYS + 1, 3: verdict_cache.MAX_AGE_DAYS - 1}
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            for i, days in ages.items():
                stamp = datetime.now(timezone.utc) - timedelta(days=days)
                conn.execute(
                    "UPDATE verdicts SET created_at = ? WHERE content_hash = ?",
                    (stamp.isoformat(timespec="microseconds"), _key(max_tokens=i)),
                )
        assert verdict_cache.lookup(scope, _key(max_tokens=1), "m") is None
        assert verdict_cache.lookup(scope, _key(max_tokens=2), "m") is None
        assert verdict_cache.lookup(scope, _key(max_tokens=3), "m") == {"v": 3}
        assert len(_rows(scope)) == 3  # a lookup only reads: nothing was pruned

    def test_a_row_exactly_at_the_age_cutoff_is_kept_by_lookup_and_prune_alike(
        self, scope, monkeypatch
    ):
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(verdict_cache, "datetime", _Frozen)
        verdict_cache.store(scope, _key(), "m", {"v": 1})
        boundary = (now - timedelta(days=verdict_cache.MAX_AGE_DAYS)).isoformat(
            timespec="microseconds"
        )
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute("UPDATE verdicts SET created_at = ?", (boundary,))
        assert verdict_cache.lookup(scope, _key(), "m") == {"v": 1}
        verdict_cache.store(scope, _key(max_tokens=2), "m", {"v": 2})  # prunes on the same clock
        assert len(_rows(scope)) == 2

    def test_lookup_on_a_database_it_cannot_age_check_is_a_miss_never_a_crash(self, scope):
        verdict_cache.store(scope, _key(max_tokens=9), "m", {"v": 9})  # creates the install key
        scope.db_path.unlink()
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute("CREATE TABLE other (x TEXT)")
        assert verdict_cache.lookup(scope, _key(), "m") is None  # no verdicts table
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE verdicts (content_hash TEXT, model TEXT, key_salt TEXT, "
                "verdict TEXT, mac TEXT)"
            )
        assert verdict_cache.lookup(scope, _key(), "m") is None  # no created_at column

    def test_the_cache_needs_no_third_party_package(self):
        """It authenticates with the standard library's HMAC; no dependency is added."""
        import inspect

        source = inspect.getsource(verdict_cache)
        assert "import hmac" in source and "cryptography" not in source
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8")
        assert "cryptography" not in pyproject


@pytest.mark.unit
class TestNeverRaises:
    def test_an_unusable_install_key_disables_the_cache_with_one_warning(self, scope, caplog):
        key_file = verdict_cache.install_key_path()
        key_file.parent.mkdir(parents=True)
        key_file.write_bytes(b"too short")
        with caplog.at_level(logging.WARNING, logger="argus"):
            for _ in range(3):
                assert verdict_cache.store(scope, _key(), "m", {"v": 1}) is False
                assert verdict_cache.lookup(scope, _key(), "m") is None
        assert len([r for r in caplog.records if "install key" in r.getMessage()]) == 1
        assert key_file.read_bytes() == b"too short", "never overwrite a key we cannot read"

    def test_an_unwritable_database_location_warns_once_and_never_raises(self, tmp_path, caplog):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        bad = verdict_cache.CacheScope(blocker / ".argus" / "v.db", "s")
        with caplog.at_level(logging.WARNING, logger="argus"):
            for _ in range(3):
                assert verdict_cache.store(bad, _key(), "m", {"v": 1}) is False
        assert len([r for r in caplog.records if "unavailable" in r.getMessage()]) == 1

    def test_a_corrupt_database_file_is_a_warned_miss(self, scope, caplog):
        scope.db_path.parent.mkdir(parents=True, exist_ok=True)
        scope.db_path.write_bytes(b"this is not an sqlite database" * 50)
        with caplog.at_level(logging.WARNING, logger="argus"):
            assert verdict_cache.lookup(scope, _key(), "m") is None
            assert verdict_cache.store(scope, _key(), "m", {"v": 1}) is False
        assert len([r for r in caplog.records if "unavailable" in r.getMessage()]) == 1

    def test_no_scope_means_no_cache_and_no_files(self, tmp_path):
        assert verdict_cache.lookup(None, _key(), "m") is None
        assert verdict_cache.store(None, _key(), "m", {"v": 1}) is False
        assert not (tmp_path / ".argus" / verdict_cache.CACHE_FILE).exists()
        assert not verdict_cache.install_key_path().exists()

    def test_a_project_that_cannot_be_located_means_no_cache_not_a_crash(
        self, monkeypatch, caplog
    ):
        def gone():
            raise FileNotFoundError("the working directory was deleted")

        monkeypatch.setattr("argus.storage.argus_dir", gone)
        with caplog.at_level(logging.WARNING, logger="argus"):
            assert verdict_cache.current_scope() is None
            assert verdict_cache.current_scope() is None
        assert len([r for r in caplog.records if "cannot locate" in r.getMessage()]) == 1

    @pytest.mark.parametrize("off", ["0", "false", "no", "OFF"])
    def test_the_kill_switch(self, monkeypatch, off):
        monkeypatch.setenv("ARGUS_VERDICT_CACHE", off)
        assert verdict_cache.current_scope() is None


@pytest.mark.unit
class TestKeyAndSalt:
    def test_the_key_is_independent_of_dict_order(self):
        a = verdict_cache.request_key({"model": "m", "temperature": 0.0, "max_tokens": 5})
        b = verdict_cache.request_key({"max_tokens": 5, "temperature": 0.0, "model": "m"})
        assert a == b

    @pytest.mark.parametrize(
        "change",
        [
            {"temperature": 0.3},
            {"max_tokens": 7},
            {"seed": 0},
            {"model": "m2"},
            {"messages": [{"role": "user", "content": "y"}]},
            {"tools": [{"type": "function"}]},
            {"tool_choice": "auto"},
            {"response_format": {"type": "json_object"}},
        ],
    )
    def test_every_part_of_the_request_is_in_the_key(self, change):
        assert _key(**change) != _key()

    def test_transport_and_unset_parameters_are_not(self):
        assert _key(timeout=5.0, api_key="sk-x") == _key()
        # a parameter left unset hashes like one that is absent: adding an
        # optional sampling parameter later never moves an unaffected key
        assert _key(seed=None, tools=None) == _key()
        assert verdict_cache.request_key({"model": "m"}, route="a:b") != verdict_cache.request_key(
            {"model": "m"}, route="c:d"
        )

    def test_the_default_salt_is_the_project_not_a_run(self, tmp_path, monkeypatch):
        first = verdict_cache.current_scope()
        second = verdict_cache.current_scope()
        assert first == second
        assert first.salt.startswith("project:")
        assert first.db_path == tmp_path.resolve() / ".argus" / "verdict_cache.db"
        monkeypatch.setenv("ARGUS_DIR", str(tmp_path / "elsewhere"))
        assert verdict_cache.current_scope().salt != first.salt

    def test_salt_precedence_is_explicit_then_env_then_project(self, monkeypatch):
        monkeypatch.setenv("ARGUS_RUN_SALT", "  from-env ")
        assert verdict_cache.resolve_salt() == "from-env"
        assert verdict_cache.resolve_salt("explicit") == "explicit"
        monkeypatch.delenv("ARGUS_RUN_SALT")
        assert verdict_cache.resolve_salt().startswith("project:")
        assert verdict_cache.current_scope("given").salt == "given"

    def test_the_route_names_the_provider_and_the_model_the_request_really_goes_to(
        self, monkeypatch
    ):
        assert judge_route("gpt-4o-mini") == "openai:gpt-4o-mini"
        monkeypatch.setattr(user_config, "get_provider", lambda: "anthropic")
        assert (
            judge_route("gpt-4o-mini")
            == f"anthropic:{providers.DEFAULT_MODELS['anthropic']['cheap']}"
        )
        monkeypatch.setattr(user_config, "get_model_overrides", lambda: {"cheap": "claude-x"})
        assert judge_route("gpt-4o-mini") == "anthropic:claude-x"
        assert judge_route("gpt-4o-mini", api_key="sk") == "openai:gpt-4o-mini"
        monkeypatch.setattr(user_config, "resolve_key", lambda provider: None)
        assert judge_route("gpt-4o-mini") == "hosted:gpt-4o-mini"


# ═════════════════════════════════════════════════════════════════════════════
# The coherence judge
# ═════════════════════════════════════════════════════════════════════════════


def _sent(call):
    """The request a fake transport call received, minus transport-only arguments."""
    return {k: v for k, v in call.items() if k not in ("timeout", "api_key")}


@pytest.mark.unit
class TestCoherenceJudge:
    def test_the_second_identical_call_is_served_from_the_cache(self, monkeypatch, scope):
        llm = _Llm(monkeypatch)
        first, _ = _judge(scope)
        second, _ = _judge(scope)

        assert len(llm.calls) == 1
        assert first.cached is False and second.cached is True
        assert (second.passed, second.reason, second.confidence, second.failure_kind) == (
            first.passed,
            first.reason,
            first.confidence,
            first.failure_kind,
        )
        assert second.prompt_tokens == 0 and second.completion_tokens == 0
        assert first.prompt_tokens == 100
        assert (second.deterministic, second.temperature, second.seed) == (True, 0.0, 0)

    def test_the_key_is_the_hash_of_the_exact_request_that_was_sent(self, monkeypatch, scope):
        """Evidence, history, sampling: whatever was sent is what is keyed."""
        llm = _Llm(monkeypatch)
        _judge(scope)
        sent = _sent(llm.calls[0])
        assert sent["temperature"] == 0.0 and sent["seed"] == 0 and sent["max_tokens"] == 150
        key = verdict_cache.request_key(sent, route=judge_route("gpt-4o-mini"))
        assert verdict_cache.lookup(scope, key, "gpt-4o-mini")["pass"] is True

    def test_without_a_scope_nothing_is_cached_or_written(self, monkeypatch, tmp_path):
        llm = _Llm(monkeypatch)
        first, _ = _judge(None)
        second, _ = _judge(None)
        assert len(llm.calls) == 2
        assert first.cached is second.cached is False
        assert not (tmp_path / ".argus" / verdict_cache.CACHE_FILE).exists()

    def test_a_verdict_ruled_with_a_failing_validator_is_not_served_to_a_call_without_one(
        self, monkeypatch, scope
    ):
        """The lookup used to run before the prompt (validator lines, history) was built."""
        failing = _tool_reply({**_VERDICT, "pass": False, "reason": "validator failed"})
        passing = _tool_reply(_VERDICT)
        llm = _Llm(monkeypatch, failing, passing)
        validator = ValidatorResult("v", False, "total must be > 0", "critical")

        with_evidence, _ = _judge(scope, validator_results=[validator])
        without, _ = _judge(scope)

        assert len(llm.calls) == 2, "different evidence is a different question"
        assert with_evidence.passed is False and without.passed is True
        assert without.cached is False
        # and each is served back to the call it was ruled for
        assert _judge(scope, validator_results=[validator])[0].passed is False
        assert _judge(scope)[0].passed is True
        assert len(llm.calls) == 2

    def test_history_and_other_evidence_are_part_of_the_question(self, monkeypatch, scope):
        llm = _Llm(monkeypatch)
        row = LedgerRow(
            step_index=0, node="earlier", input_state={}, update={"q": "was full"}, state_after={}
        )
        _judge(scope)
        _judge(scope, prior_rows=[row])
        assert len(llm.calls) == 2
        _judge(scope, prior_rows=[row])
        assert len(llm.calls) == 2

        class _Insp:
            missing_fields = ["documents"]
            tool_failures: list = []

        _judge(scope, inspection=_Insp())
        assert len(llm.calls) == 3

    def test_two_runs_of_the_same_content_share_the_row(self, monkeypatch, tmp_path):
        """The salt is the project, not a run id: a replay or a CI re-run hits."""
        llm = _Llm(monkeypatch)
        _judge(verdict_cache.current_scope())
        second, _ = _judge(verdict_cache.current_scope())
        assert len(llm.calls) == 1 and second.cached is True

    def test_another_salt_is_another_namespace(self, monkeypatch, scope):
        llm = _Llm(monkeypatch)
        _judge(scope)
        _judge(verdict_cache.current_scope("ci"))
        assert len(llm.calls) == 2

    def test_a_different_seed_is_a_different_question(self, monkeypatch, scope):
        llm = _Llm(monkeypatch)
        _judge(scope, seed=1)
        _judge(scope, seed=2)
        assert len(llm.calls) == 2
        assert _judge(scope, seed=1)[0].cached is True and len(llm.calls) == 2

    def test_the_provider_route_is_part_of_the_key(self, monkeypatch, scope):
        """The configured name is a tier hint; Anthropic/Google answer with other models."""
        llm = _Llm(monkeypatch)
        _judge(scope)
        monkeypatch.setattr(user_config, "get_provider", lambda: "google")  # also takes a seed
        _judge(scope)
        assert len(llm.calls) == 2
        monkeypatch.setattr(user_config, "get_model_overrides", lambda: {"cheap": "gemini-x"})
        _judge(scope)
        assert len(llm.calls) == 3

    def test_a_request_that_cannot_claim_it_never_touches_the_cache(self, monkeypatch, scope):
        """No seed, a non-zero temperature, or a provider with no seed: judged live, not stored."""
        llm = _Llm(monkeypatch)
        _judge(scope, seed=None)
        _judge(scope, seed=None)
        _judge(scope, temperature=0.5)
        _judge(scope, temperature=0.5)
        monkeypatch.setattr(user_config, "get_provider", lambda: "anthropic")
        _judge(scope)
        _judge(scope)
        assert len(llm.calls) == 6
        assert not scope.db_path.exists()

    @pytest.mark.parametrize(
        "kwargs", [{"seed": None}, {"temperature": 0.5}], ids=["no-seed", "temperature"]
    )
    def test_a_row_planted_for_a_request_that_cannot_claim_it_is_never_served(
        self, monkeypatch, scope, kwargs
    ):
        llm = _Llm(monkeypatch)
        _judge(scope, **kwargs)
        key = verdict_cache.request_key(_sent(llm.calls[0]), route=judge_route("gpt-4o-mini"))
        planted = {**_VERDICT, "reason": "planted"}
        assert verdict_cache.store(scope, key, "gpt-4o-mini", planted)
        again, _ = _judge(scope, **kwargs)
        assert len(llm.calls) == 2 and again.cached is False and again.reason == "fine"

    def test_the_same_planted_row_is_served_to_a_request_that_can_claim_it(
        self, monkeypatch, scope
    ):
        """The control: with a seed on a seeded route that very row is a hit."""
        llm = _Llm(monkeypatch)
        _judge(scope)
        key = verdict_cache.request_key(_sent(llm.calls[0]), route=judge_route("gpt-4o-mini"))
        assert verdict_cache.store(scope, key, "gpt-4o-mini", {**_VERDICT, "reason": "planted"})
        again, _ = _judge(scope)
        assert len(llm.calls) == 1 and again.cached is True and again.reason == "planted"

    def test_a_reply_the_provider_did_not_seed_is_not_stored(self, monkeypatch, scope):
        """The route looks seeded (OpenAI), the reply does not say the seed went out."""
        calls: list[dict] = []
        monkeypatch.setattr(
            "argus.llm_proxy.create_chat_completion",
            lambda **kw: calls.append(kw) or _tool_reply(_VERDICT),  # no `seed_honored` mark
        )
        first, _ = _judge(scope)
        second, _ = _judge(scope)
        assert len(calls) == 2 and calls[0]["seed"] == 0
        assert (first.deterministic, second.deterministic, second.cached) == (False, False, False)
        assert not scope.db_path.exists()

    def test_a_truncation_repaired_verdict_is_never_cached(self, monkeypatch, scope):
        """A reply cut after the first signal repairs to a payload that names only that one."""
        cut = (
            '{"pass": true, "reason": "ok", "confidence": 0.9, "failure_kind": "other", '
            '"evidence_considered": ["s1"'
        )
        llm = _Llm(monkeypatch, _tool_reply(cut))
        first, _ = _judge(scope)
        second, _ = _judge(scope)
        assert first.evidence_considered == ("s1",), "the repair did produce a payload"
        assert len(llm.calls) == 2 and second.cached is False
        assert not scope.db_path.exists()

    def test_fenced_or_wrapped_content_is_not_cached_but_plain_content_is(
        self, monkeypatch, scope
    ):
        body = json.dumps(_VERDICT)
        for text in (f"```json\n{body}\n```", f"Here you go: {body} done"):
            llm = _Llm(monkeypatch, _content_reply(text))
            _judge(scope)
            _judge(scope)
            assert len(llm.calls) == 2, text
        llm = _Llm(monkeypatch, _content_reply(body))
        _judge(scope)
        second, _ = _judge(scope)
        assert len(llm.calls) == 1 and second.cached is True

    @pytest.mark.parametrize(
        "payload",
        [
            {k: v for k, v in _VERDICT.items() if k != "failure_kind"},
            {k: v for k, v in _VERDICT.items() if k != "confidence"},
            {**_VERDICT, "confidence": "high"},
            {**_VERDICT, "reason": None},
            {**_VERDICT, "failure_kind": "weird"},
        ],
    )
    def test_an_incomplete_verdict_is_not_cached(self, monkeypatch, scope, payload):
        llm = _Llm(monkeypatch, _tool_reply(payload))
        _judge(scope)
        _judge(scope)
        assert len(llm.calls) == 2
        assert not scope.db_path.exists(), "an incomplete verdict is not even written"

    def test_every_ambiguous_signal_needs_a_verdict_to_be_cached(self, monkeypatch, scope):
        sigs = [
            SemanticSignal("S1", "c", "warning", "d1", ("a",), "e1", 0.5),
            SemanticSignal("S2", "c", "warning", "d2", ("a",), "e2", 0.5),
        ]
        ruled = lambda sid: {"sig_id": sid, "is_failure": False, "confidence": 0.8, "reason": "r"}  # noqa: E731
        partial_ = {**_VERDICT, "disambiguation_verdicts": [ruled("S1")]}
        llm = _Llm(monkeypatch, _tool_reply(partial_))
        _judge(scope, ambiguous_signals=sigs)
        _judge(scope, ambiguous_signals=sigs)
        assert len(llm.calls) == 2, "a reply that ruled on one of two signals is not served again"
        assert not scope.db_path.exists()

        whole = {**_VERDICT, "disambiguation_verdicts": [ruled("S1"), ruled("S2")]}
        llm = _Llm(monkeypatch, _tool_reply(whole))
        _, live = _judge(scope, ambiguous_signals=sigs)
        _, again = _judge(scope, ambiguous_signals=sigs)
        assert len(llm.calls) == 1
        assert [d.sig_id for d in again] == [d.sig_id for d in live] == ["S1", "S2"]
        assert all(d.prompt_tokens == 0 for d in again)

    def test_errors_and_skips_are_never_cached(self, monkeypatch, scope):
        llm = _Llm(monkeypatch, {"error": "provider down"})
        for _ in range(2):
            result, _ = _judge(scope)
            assert result.evaluated is False
        assert len(llm.calls) == 2
        llm = _Llm(monkeypatch, _tool_reply({"reason": "no ruling here"}))
        for _ in range(2):
            assert _judge(scope)[0].evaluated is False
        assert len(llm.calls) == 2
        assert not scope.db_path.exists()

    def test_a_foreign_or_corrupt_row_falls_through_to_a_live_judgment(self, monkeypatch, scope):
        llm = _Llm(monkeypatch)
        _judge(scope)  # learn the key
        key = verdict_cache.request_key(_sent(llm.calls[0]), route=judge_route("gpt-4o-mini"))
        for junk in ({"foo": 1}, {"pass": "maybe", "reason": "x"}, {**_VERDICT, "pass": None}):
            verdict_cache.store(scope, key, "gpt-4o-mini", junk)
            result, _ = _judge(scope)
            assert result.cached is False and result.passed is True
        assert len(llm.calls) == 4
        # the live verdict then replaced the junk
        assert _judge(scope)[0].cached is True

    def test_a_cache_that_cannot_write_never_fails_or_changes_the_judgment(
        self, monkeypatch, scope, caplog
    ):
        llm = _Llm(monkeypatch)

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(verdict_cache, "_store", boom)
        with caplog.at_level(logging.WARNING, logger="argus"):
            first, _ = _judge(scope)
            second, _ = _judge(scope)
        assert first.evaluated and second.evaluated and second.cached is False
        assert len(llm.calls) == 2
        assert len([r for r in caplog.records if "unavailable" in r.getMessage()]) == 1

    def test_a_cache_that_cannot_read_never_fails_the_judgment(self, monkeypatch, scope):
        llm = _Llm(monkeypatch)
        _judge(scope)

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(verdict_cache, "_lookup", boom)
        result, _ = _judge(scope)
        assert result.evaluated and result.cached is False and len(llm.calls) == 2

    def test_a_forged_row_is_not_served(self, monkeypatch, scope):
        """Someone with write access to the database, but not the install key."""
        llm = _Llm(monkeypatch)
        _judge(scope)
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute(
                "UPDATE verdicts SET verdict = ?",
                (json.dumps({**_VERDICT, "pass": False, "reason": "forged"}),),
            )
        result, _ = _judge(scope)
        assert result.cached is False and result.passed is True and len(llm.calls) == 2


# ═════════════════════════════════════════════════════════════════════════════
# The run reviewer
# ═════════════════════════════════════════════════════════════════════════════


def _review(monkeypatch, **wire_kwargs):
    wire = _ReviewWire(monkeypatch, **wire_kwargs)
    rec = ArgusRecorder(purposes=_PURPOSES)
    rec.attach(_graph()).invoke({"ticket": "refund A-1001"})
    record = load_run(rec.session.run_id)
    (step,) = [s for s in record.steps if s.node_name == "draft_reply"]
    return wire, record, step.review


@pytest.mark.integration
class TestRunReviewer:
    def test_a_repeat_review_is_served_from_the_cache(self, monkeypatch, scope):
        wire, _, (first,) = _review(monkeypatch, fingerprint="fp_abc", second=False)
        before = len(wire.payloads)
        assert before == 3  # the checker, the verifier, and the second model asked live
        assert (first["cached"], first["deterministic"], first["system_fingerprint"]) == (
            False,
            True,
            "fp_abc",
        )
        rec = ArgusRecorder(purposes=_PURPOSES)
        rec.attach(_graph()).invoke({"ticket": "refund A-1001"})
        (second,) = [i for s in load_run(rec.session.run_id).steps for i in s.review]
        # the checker and the verifier did not go to the provider again; the second model is
        # a reasoning model (no seed), so it is never cached and was asked live once more
        assert len(wire.payloads) == before + 1
        assert wire.payloads[-1]["model"] == "o4-mini"
        assert (second["cached"], second["deterministic"]) == (True, True)
        assert second["system_fingerprint"] == ""  # a cache hit keeps the verdict, not the reply
        assert (second["claim"], second["why"]) == (first["claim"], first["why"])

    def test_an_item_the_second_model_confirmed_is_never_wholly_cached(self, monkeypatch, scope):
        wire, _, (first,) = _review(monkeypatch, second=True)
        assert first["two_models"] is True and first["cached"] is False
        wire2 = _ReviewWire(monkeypatch, second=True)
        rec = ArgusRecorder(purposes=_PURPOSES)
        rec.attach(_graph()).invoke({"ticket": "refund A-1001"})
        (again,) = [i for s in load_run(rec.session.run_id).steps for i in s.review]
        assert again["two_models"] is True
        assert (again["cached"], again["deterministic"]) == (False, False)
        assert [p["model"] for p in wire2.payloads] == ["o4-mini"]

    def test_without_a_seed_the_reviewer_never_touches_the_cache(self, monkeypatch, scope):
        monkeypatch.delenv("ARGUS_JUDGE_SEED")
        wire, _, (item,) = _review(monkeypatch, second=False)
        _review(monkeypatch, second=False)
        assert item["cached"] is False and item["deterministic"] is False
        assert not scope.db_path.exists()

    def test_the_kill_switch_turns_the_reviewers_cache_off(self, monkeypatch, scope):
        monkeypatch.setenv("ARGUS_VERDICT_CACHE", "0")
        _review(monkeypatch, second=False)
        wire, _, (item,) = _review(monkeypatch, second=False)
        assert item["cached"] is False and len(wire.payloads) == 3
        assert not scope.db_path.exists()

    def test_a_reply_that_is_not_a_whole_answer_is_not_stored(self, monkeypatch, scope):
        class Odd(_ReviewWire):
            def _answer(self, payload):
                answer = super()._answer(payload)
                if "failures" in answer:
                    return {"summary": "no list here"}  # not a checker answer
                return answer

        Odd(monkeypatch, second=False)
        rec = ArgusRecorder(purposes=_PURPOSES)
        rec.attach(_graph()).invoke({"ticket": "x"})
        assert not scope.db_path.exists()

    def test_a_stored_row_that_is_not_a_whole_answer_is_asked_again(self, monkeypatch, scope):
        sent: list[dict] = []
        from argus import llm_proxy

        real = llm_proxy.create_chat_completion
        monkeypatch.setattr(
            "argus.llm_proxy.create_chat_completion", lambda **kw: sent.append(kw) or real(**kw)
        )
        wire, _, _ = _review(monkeypatch, second=False)
        checker = next(k for k in sent if k["messages"][0]["content"].startswith("You verify ONE"))
        key = verdict_cache.request_key(_sent(checker), route=judge_route(checker["model"]))
        assert verdict_cache.store(scope, key, checker["model"], {"unrelated": True})
        _, _, (item,) = _review(monkeypatch, second=False)
        # the junk row was not served: the checker went to the provider again
        assert item["cached"] is False
        assert (
            len(
                [
                    p
                    for p in wire.payloads
                    if p["messages"][0]["content"].startswith("You verify ONE")
                ]
            )
            == 1
        )

    def test_a_forged_checker_row_cannot_silence_the_reviewer(self, monkeypatch, scope):
        _review(monkeypatch, second=False)
        with closing(sqlite3.connect(scope.db_path)) as conn, conn:
            conn.execute("UPDATE verdicts SET verdict = ?", (json.dumps({"failures": []}),))
        _, _, items = _review(monkeypatch, second=False)
        assert len(items) == 1, "the forged row was ignored and the reviewer still reported"

    def test_other_purposes_are_another_question(self, monkeypatch, scope):
        wire = _ReviewWire(monkeypatch, second=False)
        for purposes in (_PURPOSES, {**_PURPOSES, "classify": "Does something else."}):
            ArgusRecorder(purposes=purposes).attach(_graph()).invoke({"ticket": "x"})
        checker_calls = [
            p for p in wire.payloads if p["messages"][0]["content"].startswith("You verify ONE")
        ]
        assert len(checker_calls) == 2

    def test_a_reply_the_provider_did_not_seed_is_not_stored(self, monkeypatch, scope):
        wire = _ReviewWire(monkeypatch, second=False)
        real = providers.ADAPTERS["openai"]

        def forgetful(**kwargs):
            reply = real(**kwargs)
            reply.pop("seed_honored", None)  # the seed did not go out
            return reply

        monkeypatch.setitem(providers.ADAPTERS, "openai", forgetful)
        for _ in range(2):
            ArgusRecorder(purposes=_PURPOSES).attach(_graph()).invoke({"ticket": "x"})
        assert len(wire.payloads) == 6
        assert not scope.db_path.exists()

    @pytest.mark.parametrize(
        "kwargs", [{}, {"temperature": 0.5, "seed": 3}], ids=["no-seed", "hot"]
    )
    def test_a_row_planted_for_a_call_that_cannot_claim_it_is_never_served(
        self, monkeypatch, scope, kwargs
    ):
        from argus import review

        sent: list[dict] = []

        def spy(**kw):
            sent.append(kw)
            return _like_an_adapter(
                {"choices": [{"message": {"content": json.dumps({"failures": []})}}]}, kw
            )

        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", spy)

        def ask():
            return review._complete(
                "gpt-4.1", "sys", "user", 10, cache=scope, whole=review._is_checker_reply, **kwargs
            )

        ask()
        key = verdict_cache.request_key(_sent(sent[0]), route=judge_route("gpt-4.1"))
        assert verdict_cache.store(scope, key, "gpt-4.1", {"failures": [{"node": "planted"}]})
        assert ask() == {"failures": []} and len(sent) == 2

    def test_the_same_planted_row_is_served_to_a_call_that_can_claim_it(self, monkeypatch, scope):
        """The control: seeded, temperature 0.0, on a seeded route, that row is a hit."""
        from argus import review

        sent: list[dict] = []

        def spy(**kw):
            sent.append(kw)
            return _like_an_adapter(
                {"choices": [{"message": {"content": json.dumps({"failures": []})}}]}, kw
            )

        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", spy)

        def ask():
            return review._complete(
                "gpt-4.1", "sys", "user", 10, seed=3, cache=scope, whole=review._is_checker_reply
            )

        live = ask()
        assert live.stamp["cached"] is False
        key = verdict_cache.request_key(_sent(sent[0]), route=judge_route("gpt-4.1"))
        assert verdict_cache.store(scope, key, "gpt-4.1", {"failures": [{"node": "planted"}]})
        served = ask()
        assert len(sent) == 1 and served == {"failures": [{"node": "planted"}]}
        assert (served.stamp["cached"], served.stamp["deterministic"]) == (True, True)

    def test_a_stubbed_transport_is_called_without_cache_arguments_when_the_cache_is_off(
        self, monkeypatch
    ):
        seen: list[dict] = []

        def fake(model, system, user, max_tokens, **kw):
            seen.append(kw)
            return {"failures": []}

        monkeypatch.setattr("argus.review._complete", fake)
        Reviewer(purposes={"a": "b"}, seed=0)([], [])
        assert seen == [{"seed": 0}]
        Reviewer(purposes={"a": "b"}, seed=0, cache=verdict_cache.current_scope())([], [])
        assert set(seen[1]) == {"seed", "cache", "whole"}

    def test_the_recorder_gives_the_reviewer_a_scope_only_when_it_is_seeded(self, monkeypatch):
        seeded = ArgusRecorder(purposes=_PURPOSES)._resolve_reviewer()
        assert seeded.cache == verdict_cache.current_scope()
        monkeypatch.delenv("ARGUS_JUDGE_SEED")
        assert ArgusRecorder(purposes=_PURPOSES)._resolve_reviewer().cache is None
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "1")
        monkeypatch.setenv("ARGUS_VERDICT_CACHE", "0")
        assert ArgusRecorder(purposes=_PURPOSES)._resolve_reviewer().cache is None


# ═════════════════════════════════════════════════════════════════════════════
# Through a real session
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestThroughASession:
    def _step(self, config):
        from test_determinism import _run_step

        return _run_step(config)

    def test_a_seeded_session_serves_the_repeat_from_the_cache(self, monkeypatch):
        wire = _Wire(monkeypatch, "openai", fingerprint="fp_abc")
        config = ArgusConfig(on_judge_failure="warn", judge_seed=0)
        _, first = self._step(config)
        record, second = self._step(config)
        assert wire.calls == 1
        assert (first.cached, first.deterministic) == (False, True)
        assert (second.cached, second.deterministic) == (True, True)
        assert first.system_fingerprint == "fp_abc" and second.system_fingerprint == ""
        assert load_run(record.run_id).steps[0].semantic_check.cached is True

    def test_the_default_session_judges_live_every_time(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ARGUS_JUDGE_SEED")
        wire = _Wire(monkeypatch, "openai")
        self._step(None)
        _, second = self._step(None)
        assert wire.calls == 2 and second.cached is False
        assert not (tmp_path / ".argus" / verdict_cache.CACHE_FILE).exists()

    def test_the_kill_switch_beats_a_seed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARGUS_VERDICT_CACHE", "0")
        wire = _Wire(monkeypatch, "openai")
        config = ArgusConfig(on_judge_failure="warn", judge_seed=0)
        self._step(config)
        self._step(config)
        assert wire.calls == 2
        assert not (tmp_path / ".argus" / verdict_cache.CACHE_FILE).exists()

    def test_a_seed_dropping_provider_is_never_cached(self, monkeypatch, tmp_path):
        wire = _Wire(monkeypatch, "anthropic")
        config = ArgusConfig(on_judge_failure="warn", judge_seed=0)
        self._step(config)
        _, second = self._step(config)
        assert wire.calls == 2 and second.cached is False
        assert not (tmp_path / ".argus" / verdict_cache.CACHE_FILE).exists()

    def test_the_cached_flag_survives_the_run_file_and_an_old_file_loads(
        self, monkeypatch, tmp_path
    ):
        _Wire(monkeypatch, "openai")
        config = ArgusConfig(on_judge_failure="warn", judge_seed=0)
        self._step(config)
        record, _ = self._step(config)
        path = tmp_path / ".argus" / "runs" / f"{record.run_id}.json"
        data = json.loads(path.read_text("utf-8"))
        assert data["steps"][0]["semantic_check"]["cached"] is True
        del data["steps"][0]["semantic_check"]["cached"]
        path.write_text(json.dumps(data), "utf-8")
        assert load_run(record.run_id).steps[0].semantic_check.cached is False
