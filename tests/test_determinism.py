"""Determinism stamps for the two LLM judges that rule on a run.

A judge verdict may call itself *deterministic* only when it asked for it (temperature
0.0 and a seed), its route can carry a seed, and the live reply says the seed went out.
Everything else is judged live and stamped non-deterministic. The two judges are the
per-step coherence judge (`SemanticCheckResult`) and the run reviewer (each verified
item in `NodeEvent.review`). `deterministic` is audit metadata: no gate reads it.

The HTTP layer is faked (`providers._post_json`); the adapters, `create_chat_completion`,
the judges and the stamp are the real ones. No test reaches a network.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TypedDict

import pytest
from conftest import make_event, make_run_record
from langgraph.graph import END, START, StateGraph
from typer.testing import CliRunner

from argus import ArgusRecorder, determinism, providers, user_config
from argus.check import evaluate_run
from argus.cli.main import app
from argus.models import ArgusConfig, LLMInvestigationConfig, SemanticCheckResult
from argus.review import Reviewer
from argus.semantic_checker import check_semantic_coherence
from argus.storage import load_run, save_run
from argus.watcher import ArgusWatcher

# ── the fake HTTP layer ──────────────────────────────────────────────────────

_COHERENCE = {
    "pass": True,
    "reason": "fine",
    "confidence": 0.9,
    "failure_kind": "other",
    "evidence_considered": [],
    "overridden_signals": [],
    "disambiguation_verdicts": [],
}


def _tool_name(provider, payload):
    tool = payload["tools"][0]
    if provider == "google":
        return tool["functionDeclarations"][0]["name"]
    return tool["function"]["name"] if provider == "openai" else tool["name"]


def _route_to(monkeypatch, provider):
    monkeypatch.setattr(user_config, "get_provider", lambda: provider)
    monkeypatch.setattr(user_config, "resolve_key", lambda p: "k")
    monkeypatch.setattr(user_config, "get_model_overrides", lambda: {})


class _Wire:
    """Stands in for `providers._post_json` for the coherence judge: forced-tool replies."""

    def __init__(self, monkeypatch, provider, *, fingerprint=None):
        self.provider, self.fingerprint = provider, fingerprint
        self.payloads: list[dict] = []
        _route_to(monkeypatch, provider)
        monkeypatch.setattr(providers, "_post_json", self)

    def __call__(self, url, headers, payload, timeout, label):
        self.payloads.append(payload)
        name = _tool_name(self.provider, payload)
        if self.provider == "anthropic":
            return {
                "content": [{"type": "tool_use", "id": "c", "name": name, "input": _COHERENCE}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        if self.provider == "google":
            part = {"functionCall": {"name": name, "args": _COHERENCE}}
            return {
                "candidates": [{"content": {"parts": [part]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            }
        call = {
            "id": "c",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(_COHERENCE)},
        }
        body = {
            "choices": [{"message": {"content": None, "tool_calls": [call]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        if self.fingerprint:
            body["system_fingerprint"] = self.fingerprint
        return body

    @property
    def calls(self):
        return len(self.payloads)


class _ReviewWire:
    """`providers._post_json` for the run reviewer: plain JSON-object replies, by prompt."""

    def __init__(self, monkeypatch, provider="openai", *, fingerprint=None, second=True):
        self.provider, self.fingerprint, self.second = provider, fingerprint, second
        self.payloads: list[dict] = []
        self.urls: list[str] = []
        _route_to(monkeypatch, provider)
        monkeypatch.setattr(providers, "_post_json", self)

    def _answer(self, payload, url):
        # Anthropic and Gemini carry the system prompt in their own fields
        gemini = (payload.get("systemInstruction") or {}).get("parts", [{}])[0].get("text")
        system = payload.get("system") or gemini or payload["messages"][0]["content"]
        if system.startswith("You verify ONE run"):  # the checker
            return {
                "failures": [
                    {
                        "node": "draft_reply",
                        "kind": "contradicted",
                        "claim": "x",
                        "evidence": "y",
                        "why": "the reply contradicts its evidence",
                        "confidence": 0.9,
                    }
                ]
            }
        # a verifier; the second model is the reasoning model, o4-mini
        model = payload.get("model", "") + url  # Gemini names the model in the URL
        second = model.startswith("o4") or "haiku" in model or "flash" in model
        real = self.second if second else True
        return {"real_defect": real, "correct_value": "the exact fix" if real else None}

    def __call__(self, url, headers, payload, timeout, label):
        self.payloads.append(payload)
        self.urls.append(url)
        text = json.dumps(self._answer(payload, url))
        if self.provider == "anthropic":
            return {
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        if self.provider == "google":
            return {
                "candidates": [{"content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            }
        body = {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        if self.fingerprint:
            body["system_fingerprint"] = self.fingerprint
        return body


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ARGUS_JUDGE_SEED", raising=False)
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)


def _coherence(**kw):
    return check_semantic_coherence(
        "n1", {"q": "hello"}, {"a": "world"}, model="gpt-4o-mini", **kw
    )[0]


def _sent_temperature_and_seed(payload, provider):
    config = payload["generationConfig"] if provider == "google" else payload
    return config["temperature"], config.get("seed")


# ═════════════════════════════════════════════════════════════════════════════
# The adapters: what goes on the wire, and what the reply says about it
# ═════════════════════════════════════════════════════════════════════════════


def _adapter(monkeypatch, call, **kwargs):
    seen: list[dict] = []

    def post(url, headers, payload, timeout, label):
        seen.append(payload)
        return {"choices": [{"message": {"content": "{}"}}], "system_fingerprint": "fp_1"}

    monkeypatch.setattr(providers, "_post_json", post)
    reply = call(api_key="k", model=kwargs.pop("model", "gpt-4o"), messages=[], **kwargs)
    return seen[0], reply


@pytest.mark.unit
class TestAdapters:
    def test_openai_sends_the_seed_and_marks_the_reply(self, monkeypatch):
        payload, reply = _adapter(monkeypatch, providers.call_openai, temperature=0.0, seed=7)
        assert payload["seed"] == 7 and payload["temperature"] == 0.0
        assert reply["seed_honored"] is True
        assert reply["system_fingerprint"] == "fp_1"  # OpenAI's own build id passes through

    def test_openai_without_a_seed_sends_none_and_marks_nothing(self, monkeypatch):
        payload, reply = _adapter(monkeypatch, providers.call_openai, temperature=0.0)
        assert "seed" not in payload and "seed_honored" not in reply

    def test_an_openai_reasoning_model_gets_no_seed_and_no_mark(self, monkeypatch):
        payload, reply = _adapter(
            monkeypatch, providers.call_openai, model="o3-mini", temperature=0.0, seed=7
        )
        assert "seed" not in payload and "temperature" not in payload
        assert "seed_honored" not in reply

    def test_an_openai_error_is_not_marked(self, monkeypatch):
        monkeypatch.setattr(providers, "_post_json", lambda *a: {"error": "429"})
        reply = providers.call_openai(api_key="k", model="gpt-4o", messages=[], seed=7)
        assert reply == {"error": "429"}

    def test_anthropic_has_no_seed_so_its_reply_is_never_marked(self, monkeypatch):
        seen: list[dict] = []

        def post(url, headers, payload, timeout, label):
            seen.append(payload)
            return {"content": [{"type": "text", "text": "{}"}], "usage": {}}

        monkeypatch.setattr(providers, "_post_json", post)
        reply = providers.call_anthropic(
            api_key="k", model="claude-x", messages=[{"role": "user", "content": "x"}], seed=7
        )
        assert "seed" not in seen[0] and "seed_honored" not in reply

    @pytest.mark.parametrize("seed", [7, 0, None])
    def test_gemini_carries_the_seed_in_the_generation_config(self, monkeypatch, seed):
        seen: list[dict] = []

        def post(url, headers, payload, timeout, label):
            seen.append(payload)
            return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}

        monkeypatch.setattr(providers, "_post_json", post)
        reply = providers.call_google(
            api_key="k", model="gemini-x", messages=[{"role": "user", "content": "x"}], seed=seed
        )
        config = seen[0]["generationConfig"]
        assert config.get("seed") == seed
        assert ("seed_honored" in reply) is (seed is not None)

    @pytest.mark.parametrize(
        ("provider", "model", "honored"),
        [
            ("openai", "gpt-4o", True),
            ("openai", "gpt-4o-mini", True),
            ("openai", "o3-mini", False),
            ("google", "gemini-2.5-flash", True),
            ("anthropic", "claude-sonnet-4-5", False),
            ("hosted", "gpt-4o", False),
            ("", "", False),
        ],
    )
    def test_which_routes_can_carry_a_seed(self, provider, model, honored):
        assert providers.honors_seed(provider, model) is honored

    def test_create_chat_completion_hands_an_adapter_a_seed_only_when_one_is_set(
        self, monkeypatch
    ):
        _route_to(monkeypatch, "openai")
        seen: list[dict] = []
        monkeypatch.setitem(providers.ADAPTERS, "openai", lambda **kw: seen.append(kw) or {})
        from argus.llm_proxy import create_chat_completion

        create_chat_completion(model="m", messages=[])
        create_chat_completion(model="m", messages=[], seed=3)
        assert "seed" not in seen[0] and seen[1]["seed"] == 3

    def test_the_hosted_proxy_is_sent_no_seed_and_never_marks_a_reply(self, monkeypatch):
        monkeypatch.setattr(user_config, "get_provider", lambda: "openai")
        monkeypatch.setattr(user_config, "resolve_key", lambda p: None)
        seen: list[dict] = []
        monkeypatch.setattr("argus.llm_proxy._call_proxy", lambda **kw: seen.append(kw) or {})
        from argus.llm_proxy import create_chat_completion

        assert create_chat_completion(model="m", messages=[], seed=3) == {}
        assert "seed" not in seen[0]


# ═════════════════════════════════════════════════════════════════════════════
# The coherence judge: the request and the stamp
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestTheCoherenceJudge:
    @pytest.mark.parametrize("provider", ["openai", "google"])
    def test_a_pinned_request_on_a_seeded_provider_is_deterministic(self, monkeypatch, provider):
        wire = _Wire(monkeypatch, provider, fingerprint="fp_abc")
        verdict = _coherence(seed=7)
        assert wire.calls == 1
        assert _sent_temperature_and_seed(wire.payloads[0], provider) == (0.0, 7)
        assert verdict.deterministic is True
        assert (verdict.temperature, verdict.seed) == (0.0, 7)
        assert verdict.system_fingerprint == ("fp_abc" if provider == "openai" else "")

    def test_anthropic_is_never_deterministic(self, monkeypatch):
        wire = _Wire(monkeypatch, "anthropic")
        verdict = _coherence(seed=7)
        assert "seed" not in wire.payloads[0]  # the API has none to send
        assert verdict.deterministic is False
        assert (verdict.temperature, verdict.seed) == (0.0, 7)  # what was asked is recorded

    def test_without_a_seed_nothing_is_sent_and_the_verdict_says_so(self, monkeypatch):
        wire = _Wire(monkeypatch, "openai")
        verdict = _coherence()
        assert "seed" not in wire.payloads[0]
        assert verdict.deterministic is False
        assert (verdict.temperature, verdict.seed) == (0.0, None)

    def test_a_non_zero_temperature_is_honoured_and_not_deterministic(self, monkeypatch):
        wire = _Wire(monkeypatch, "openai")
        verdict = _coherence(temperature=0.7, seed=7)
        assert _sent_temperature_and_seed(wire.payloads[0], "openai") == (0.7, 7)
        assert verdict.deterministic is False
        assert (verdict.temperature, verdict.seed) == (0.7, 7)

    def test_a_provider_that_drops_the_seed_is_not_deterministic(self, monkeypatch):
        """The route looks seeded (OpenAI), the reply does not say the seed went out."""
        wire = _Wire(monkeypatch, "openai")
        seen: list[dict] = []

        def forgetful(**kwargs):
            seen.append(kwargs)
            return wire(None, None, {"tools": kwargs["tools"]}, 1, "x")  # no `seed_honored`

        monkeypatch.setitem(providers.ADAPTERS, "openai", forgetful)
        verdict = _coherence(seed=7)
        assert seen[0]["seed"] == 7 and verdict.deterministic is False

    def test_int_zero_and_float_zero_are_one_request(self, monkeypatch):
        wire = _Wire(monkeypatch, "openai")
        _coherence(temperature=0, seed=7)
        _coherence(temperature=0.0, seed=7)
        assert wire.payloads[0] == wire.payloads[1]
        assert isinstance(wire.payloads[0]["temperature"], float)

    def test_the_default_request_carries_no_seed_and_an_old_stub_still_binds(self, monkeypatch):
        """Unseeded, the request is what it always was: no `seed`, `temperature` 0.0."""
        sent: list[dict] = []

        def old_stub(
            *,
            model,
            messages,
            max_tokens,
            temperature,
            tools,
            tool_choice,
            timeout,
            response_format=None,
            api_key=None,
        ):
            sent.append({"temperature": temperature})
            return {"error": "no network"}

        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", old_stub)
        result = _coherence()
        assert sent == [{"temperature": 0.0}] and isinstance(sent[0]["temperature"], float)
        assert result.deterministic is False

    def test_the_hosted_proxy_is_never_deterministic(self, monkeypatch):
        monkeypatch.setattr(user_config, "get_provider", lambda: "openai")
        monkeypatch.setattr(user_config, "resolve_key", lambda p: None)
        sent: list[dict] = []

        def proxy(**kwargs):
            sent.append(kwargs)
            return {"choices": [{"message": {"content": json.dumps(_COHERENCE)}}], "usage": {}}

        monkeypatch.setattr("argus.llm_proxy._call_proxy", proxy)
        verdict = _coherence(seed=3)
        assert "seed" not in sent[0] and verdict.deterministic is False

    def test_a_skipped_judge_claims_nothing(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)
        sc = _coherence(seed=7)
        assert (sc.evaluated, sc.deterministic, sc.seed) == (False, False, None)


# ═════════════════════════════════════════════════════════════════════════════
# The run reviewer: each verified item says how reproducible it is
# ═════════════════════════════════════════════════════════════════════════════

_PURPOSES = {"classify": "Extracts the order id.", "draft_reply": "Writes the customer reply."}


class _S(TypedDict, total=False):
    ticket: str
    order_id: str
    reply: str


def _graph():
    def classify(s: _S) -> dict:
        return {"order_id": "A-1001"}

    def draft_reply(s: _S) -> dict:
        return {"reply": "Your refund is on its way."}

    g = StateGraph(_S)
    g.add_node("classify", classify)
    g.add_node("draft_reply", draft_reply)
    g.add_edge(START, "classify")
    g.add_edge("classify", "draft_reply")
    g.add_edge("draft_reply", END)
    return g.compile()


def _review(**recorder_kwargs):
    """Run the graph under the reviewer; the reviewer's items on the node it reported."""
    rec = ArgusRecorder(purposes=_PURPOSES, **recorder_kwargs)
    rec.attach(_graph()).invoke({"ticket": "refund A-1001"})
    record = load_run(rec.session.run_id)
    (step,) = [s for s in record.steps if s.node_name == "draft_reply"]
    return record, step.review


@pytest.mark.integration
class TestTheRunReviewer:
    def test_a_seeded_reviewer_on_openai_stamps_its_item_deterministic(self, monkeypatch):
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "7")
        wire = _ReviewWire(monkeypatch, fingerprint="fp_abc", second=False)
        record, items = _review()
        (item,) = items
        assert item["two_models"] is False
        assert (item["deterministic"], item["temperature"], item["seed"]) == (True, 0.0, 7)
        assert item["system_fingerprint"] == "fp_abc"
        # the checker and the verifier ran seeded; the second model (o4-mini) was asked too
        assert [(p["model"], p.get("seed")) for p in wire.payloads] == [
            ("gpt-4.1", 7),
            ("gpt-4.1", 7),
            ("o4-mini", None),
        ]
        assert determinism.status(record) == "deterministic"

    def test_an_item_the_second_model_confirmed_is_not_deterministic(self, monkeypatch):
        """o4-mini takes no temperature and no seed, and its call is part of what fails CI."""
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "7")
        _ReviewWire(monkeypatch, second=True)
        _, (item,) = _review()
        assert item["two_models"] is True
        assert item["deterministic"] is False
        assert (item["temperature"], item["seed"]) == (0.0, 7)

    def test_without_a_seed_the_item_says_it_was_not_pinned(self, monkeypatch):
        wire = _ReviewWire(monkeypatch, second=False)
        _, (item,) = _review()
        assert all("seed" not in p for p in wire.payloads)
        assert (item["deterministic"], item["temperature"], item["seed"]) == (False, 0.0, None)

    def test_anthropic_cannot_carry_a_seed(self, monkeypatch):
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "7")
        wire = _ReviewWire(monkeypatch, "anthropic", second=False)
        _, (item,) = _review()
        assert all("seed" not in p for p in wire.payloads)
        assert (item["deterministic"], item["seed"]) == (False, 7)

    @pytest.mark.parametrize("forgetful_call", [1, 2], ids=["the-checker", "the-verifier"])
    def test_one_call_that_was_not_seeded_makes_the_item_non_deterministic(
        self, monkeypatch, forgetful_call
    ):
        """The stamp is the AND of the checker's and the verifier's calls."""
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "7")
        wire = _ReviewWire(monkeypatch, second=False)
        real = providers.ADAPTERS["openai"]
        calls: list[dict] = []

        def second_call_forgets(**kwargs):
            calls.append(kwargs)
            reply = real(**kwargs)
            if len(calls) == forgetful_call:  # this call's seed did not go out
                reply.pop("seed_honored", None)
            return reply

        monkeypatch.setitem(providers.ADAPTERS, "openai", second_call_forgets)
        _, (item,) = _review()
        assert len(wire.payloads) == 3 and calls[0]["seed"] == 7
        assert item["deterministic"] is False

    def test_a_reviewer_stubbed_without_a_stamp_records_none(self, monkeypatch):
        """A fake `_complete` that returns a bare dict leaves the item unstamped, not crashed."""

        def fake(model, system, user, max_tokens):
            if system.startswith("You verify ONE run"):
                return {
                    "failures": [
                        {"node": "draft_reply", "kind": "k", "claim": "x", "confidence": 0.9}
                    ]
                }
            return {"real_defect": True, "correct_value": "fix"}

        monkeypatch.setattr("argus.review._complete", fake)
        _, (item,) = _review()
        assert (item["deterministic"], item["temperature"], item["seed"]) == (False, None, None)

    def test_the_sampling_is_passed_to_the_transport_only_when_it_is_set(self, monkeypatch):
        seen: list[dict] = []

        def fake(model, system, user, max_tokens, **kw):
            seen.append(kw)
            return {"failures": []}

        monkeypatch.setattr("argus.review._complete", fake)
        Reviewer(purposes={"a": "b"})([], [])
        Reviewer(purposes={"a": "b"}, temperature=0.5, seed=3)([], [])
        assert seen == [{}, {"temperature": 0.5, "seed": 3}]

    def test_the_recorder_gives_the_reviewer_the_environment_seed(self, monkeypatch):
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "12")
        rec = ArgusRecorder(purposes=_PURPOSES)
        reviewer = rec._resolve_reviewer()
        assert (reviewer.temperature, reviewer.seed) == (0.0, 12)
        monkeypatch.delenv("ARGUS_JUDGE_SEED")
        reviewer = ArgusRecorder(purposes=_PURPOSES)._resolve_reviewer()
        assert (reviewer.temperature, reviewer.seed) == (0.0, None)

    def test_the_stamp_survives_the_run_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "7")
        _ReviewWire(monkeypatch, fingerprint="fp_abc", second=False)
        record, _ = _review()
        loaded = load_run(record.run_id)
        (item,) = [i for s in loaded.steps for i in s.review]
        assert (item["deterministic"], item["seed"], item["system_fingerprint"]) == (
            True,
            7,
            "fp_abc",
        )

    def test_the_stamp_moves_no_gate(self, monkeypatch):
        """Seeded or not, the reviewer's item gates (or does not gate) the same way."""
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "7")
        _ReviewWire(monkeypatch, second=True)
        seeded, _ = _review()
        monkeypatch.delenv("ARGUS_JUDGE_SEED")
        unseeded, _ = _review()
        assert evaluate_run(seeded).passed is evaluate_run(unseeded).passed
        assert seeded.overall_status == unseeded.overall_status

    def test_a_deterministic_item_the_second_model_confirmed_still_fails_ci(self, monkeypatch):
        """On a Gemini key the second model is seeded too, so the item is deterministic; it
        gates exactly like the non-deterministic one an OpenAI key gives."""
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "7")
        wire = _ReviewWire(monkeypatch, "google", second=True)
        record, (item,) = _review()
        assert [("flash" in url) for url in wire.urls] == [False, False, True]
        assert item["two_models"] is True and item["deterministic"] is True
        assert evaluate_run(record).passed is False
        monkeypatch.delenv("ARGUS_JUDGE_SEED")
        unseeded, (loose,) = _review()
        assert loose["deterministic"] is False
        assert evaluate_run(unseeded).passed is False
        assert unseeded.overall_status == record.overall_status


# ═════════════════════════════════════════════════════════════════════════════
# The config: one source, validated, never clobbered
# ═════════════════════════════════════════════════════════════════════════════

_BAD_TEMPERATURES = [float("nan"), float("inf"), -0.1, 2.5, 3.0, True, "0", [0.0]]
_BAD_SEEDS = [2**70, -(2**70), 2**63, True, "0", 1.5, [1]]


@pytest.mark.unit
class TestConfig:
    @pytest.mark.parametrize("value", _BAD_TEMPERATURES, ids=repr)
    def test_a_bad_temperature_is_a_value_error(self, value):
        with pytest.raises(ValueError, match="judge_temperature"):
            ArgusConfig(judge_temperature=value)

    @pytest.mark.parametrize("value", _BAD_SEEDS, ids=repr)
    def test_a_bad_seed_is_a_value_error(self, value):
        with pytest.raises(ValueError, match="judge_seed"):
            ArgusConfig(judge_seed=value)

    def test_every_problem_is_reported_at_once(self):
        with pytest.raises(ValueError) as excinfo:
            ArgusConfig(judge_temperature=3.0, judge_seed=2**70)
        assert "judge_temperature" in str(excinfo.value) and "judge_seed" in str(excinfo.value)

    @pytest.mark.parametrize("temperature", [None, 0, 0.0, 1, 2.0])
    @pytest.mark.parametrize("seed", [None, 0, -5, 2**63 - 1])
    def test_good_values_are_accepted(self, temperature, seed):
        ArgusConfig(judge_temperature=temperature, judge_seed=seed)

    def test_the_defaults_send_no_seed(self):
        assert (ArgusConfig().judge_temperature, ArgusConfig().judge_seed) == (None, None)
        assert determinism.resolve_sampling() == (0.0, None)

    def test_an_arguscfg_value_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "5")
        assert determinism.resolve_sampling() == (0.0, 5)
        assert determinism.resolve_sampling(ArgusConfig(judge_seed=0)) == (0.0, 0)
        assert determinism.resolve_sampling(ArgusConfig(judge_temperature=0.4)) == (0.4, 5)

    @pytest.mark.parametrize("raw", ["abc", "1.5", str(2**70), "  "])
    def test_an_unusable_environment_seed_means_no_seed(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("ARGUS_JUDGE_SEED", raw)
        with caplog.at_level(logging.WARNING, logger="argus"):
            assert determinism.resolve_sampling() == (0.0, None)
        assert bool(raw.strip()) is any("ARGUS_JUDGE_SEED" in r.message for r in caplog.records)

    def test_the_session_defaults_to_no_seed(self):
        from argus.session import ArgusSession

        session = ArgusSession(llm_investigation=LLMInvestigationConfig(enabled=True))
        assert session._sampling == (0.0, None)

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    def test_the_watcher_takes_the_settings_and_validates_them(self):
        watcher = ArgusWatcher(judge_temperature=0, judge_seed=5)
        assert (watcher._config.judge_temperature, watcher._config.judge_seed) == (0, 5)
        with pytest.raises(ValueError, match="judge_seed"):
            ArgusWatcher(judge_seed="0")


# ═════════════════════════════════════════════════════════════════════════════
# Through a real session, and into the run file
# ═════════════════════════════════════════════════════════════════════════════

_TODO_OUT = {"summary": "Q3 revenue grew 12%.", "notes": "TODO"}  # PH-001: the judge is asked


def _run_step(config=None):
    from argus.session import ArgusSession

    session = ArgusSession(
        llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True),
        config=config or ArgusConfig(on_judge_failure="warn"),
    )
    wrapped = session.wrap("summarize", lambda state: dict(_TODO_OUT))
    session.set_node_names(["summarize"])
    session.set_edges({})
    wrapped({"query": "q"})
    session.finalize()
    record = load_run(session.run_id)
    return record, record.steps[0].semantic_check


@pytest.mark.integration
class TestThroughASession:
    def test_a_seeded_session_is_deterministic_and_recorded(self, monkeypatch):
        wire = _Wire(monkeypatch, "openai", fingerprint="fp_abc")
        record, verdict = _run_step(ArgusConfig(on_judge_failure="warn", judge_seed=0))
        assert _sent_temperature_and_seed(wire.payloads[0], "openai") == (0.0, 0)
        assert verdict.deterministic is True
        assert (verdict.temperature, verdict.seed) == (0.0, 0)
        assert verdict.system_fingerprint == "fp_abc"
        assert determinism.status(record) == "deterministic"

    def test_the_default_session_judges_unseeded_and_says_so(self, monkeypatch):
        wire = _Wire(monkeypatch, "openai")
        _, verdict = _run_step()
        assert all("seed" not in p for p in wire.payloads)
        assert verdict.deterministic is False
        assert (verdict.temperature, verdict.seed) == (0.0, None)

    def test_arguscfg_and_the_environment_reach_the_judge(self, monkeypatch):
        wire = _Wire(monkeypatch, "openai")
        monkeypatch.setenv("ARGUS_JUDGE_SEED", "4")
        _, via_env = _run_step()
        monkeypatch.delenv("ARGUS_JUDGE_SEED")
        _, via_config = _run_step(ArgusConfig(on_judge_failure="warn", judge_seed=6))
        assert (via_env.seed, via_config.seed) == (4, 6)
        assert [p.get("seed") for p in wire.payloads] == [4, 6]

    def test_a_seed_dropping_provider_is_stamped_non_deterministic(self, monkeypatch):
        _Wire(monkeypatch, "anthropic")
        _, verdict = _run_step(ArgusConfig(on_judge_failure="warn", judge_seed=0))
        assert verdict.deterministic is False

    def test_the_sampling_survives_the_run_file_and_an_old_file_loads(self, monkeypatch, tmp_path):
        _Wire(monkeypatch, "openai", fingerprint="fp_abc")
        record, _ = _run_step(ArgusConfig(on_judge_failure="warn", judge_seed=3))
        loaded = load_run(record.run_id).steps[0].semantic_check
        assert (loaded.deterministic, loaded.temperature, loaded.seed) == (True, 0.0, 3)
        assert loaded.system_fingerprint == "fp_abc"
        path = tmp_path / ".argus" / "runs" / f"{record.run_id}.json"
        data = json.loads(path.read_text("utf-8"))
        for field in ("deterministic", "temperature", "seed", "system_fingerprint"):
            del data["steps"][0]["semantic_check"][field]
        path.write_text(json.dumps(data), "utf-8")
        old = load_run(record.run_id).steps[0].semantic_check
        assert (old.deterministic, old.temperature, old.seed, old.system_fingerprint) == (
            False,
            None,
            None,
            "",
        )

    def test_a_hand_edited_run_file_with_junk_in_the_fields_still_loads(
        self, monkeypatch, tmp_path
    ):
        _Wire(monkeypatch, "openai")
        record, _ = _run_step(ArgusConfig(on_judge_failure="warn", judge_seed=3))
        path = tmp_path / ".argus" / "runs" / f"{record.run_id}.json"
        data = json.loads(path.read_text("utf-8"))
        data["steps"][0]["semantic_check"].update(
            deterministic="yes", temperature="hot", seed=True, system_fingerprint=7
        )
        path.write_text(json.dumps(data), "utf-8")
        junk = load_run(record.run_id).steps[0].semantic_check
        assert (junk.deterministic, junk.temperature, junk.seed, junk.system_fingerprint) == (
            False,
            None,
            None,
            "",
        )

    def test_show_names_a_seed_the_provider_dropped(self, monkeypatch):
        _Wire(monkeypatch, "anthropic")
        record, _ = _run_step(ArgusConfig(on_judge_failure="warn", judge_seed=0))
        _, out = _out("show", record.run_id)
        assert "non-deterministic (temp=0.0, seed=0, seed not honored by the provider)" in out


# ═════════════════════════════════════════════════════════════════════════════
# Reporting: show / check / diff, and never a gate
# ═════════════════════════════════════════════════════════════════════════════


def _sc(**kw):
    return SemanticCheckResult(
        passed=True,
        reason="ok",
        confidence=0.9,
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        duration_ms=1.0,
        **kw,
    )


def _judged(run_id, *checks, reviewed=(), status="clean"):
    """A saved run with one step per coherence check, plus a step carrying reviewer items."""
    events = [
        make_event(node_name=f"n{i}", step_index=i, output={"a": 1}, semantic_check=c)
        for i, c in enumerate(checks)
    ]
    if reviewed:
        event = make_event(node_name="reviewed", step_index=len(events), output={"a": 1})
        event.review = [{"kind": "k", "role": "advisory", **r} for r in reviewed]
        events.append(event)
    record = make_run_record(events=events or [make_event()], status=status, run_id=run_id)
    save_run(record)
    return record


_DET = {"deterministic": True, "temperature": 0.0, "seed": 42}
_UNSEEDED = {"deterministic": False, "temperature": 0.0, "seed": None}
_DROPPED = {"deterministic": False, "temperature": 0.0, "seed": 42}


def _out(*args):
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, " ".join(result.output.split())


@pytest.mark.unit
class TestReporting:
    def test_show_reports_a_deterministic_run(self):
        _judged("r1", _sc(**_DET), reviewed=[_DET])
        code, out = _out("show", "r1")
        assert code == 0 and "judge determinism: deterministic (temp=0.0, seed=42)" in out

    def test_a_reviewer_item_alone_is_reported(self):
        _judged("r1", reviewed=[_UNSEEDED])
        _, out = _out("show", "r1")
        assert "judge determinism: non-deterministic (temp=0.0, no seed)" in out

    def test_show_reports_a_non_deterministic_run_and_why(self):
        _judged("r1", _sc(**_UNSEEDED), _sc(**_DROPPED))
        _, out = _out("show", "r1")
        assert "judge determinism: non-deterministic (temp=0.0, no seed;" in out
        assert "seed not honored by the provider" in out

    def test_show_reports_a_mixed_run(self):
        _judged("r1", _sc(**_DET), reviewed=[_UNSEEDED])
        _, out = _out("show", "r1")
        assert (
            "judge determinism: mixed (1 of 2 verdicts non-deterministic: temp=0.0, no seed)"
            in out
        )

    def test_show_says_nothing_when_nothing_was_judged(self):
        _judged("r1", _sc(evaluated=False), None)
        _, out = _out("show", "r1")
        assert "judge determinism" not in out

    def test_an_older_record_is_reported_as_unrecorded_not_as_deterministic(self):
        _judged("r1", _sc())  # no sampling fields: a record from before this
        _, out = _out("show", "r1")
        assert "non-deterministic (sampling not recorded)" in out

    def test_the_fingerprint_is_shown_and_escaped(self):
        _judged("r1", _sc(**_DET, system_fingerprint="fp_[red]x[/red]"))
        code, out = _out("show", "r1")
        assert code == 0 and "system_fingerprint fp_[red]x[/red]" in out
        code, out = _out("check", "r1")
        assert code == 0 and "system_fingerprint fp_[red]x[/red]" in out

    def test_a_reviewers_joined_fingerprints_are_listed_once_each(self):
        _judged("r1", reviewed=[{**_DET, "system_fingerprint": "fp_a,fp_b"}, _DET])
        _, out = _out("show", "r1")
        assert "system_fingerprint fp_a, fp_b" in out

    def test_check_reports_it_in_text_and_json(self):
        _judged("r1", _sc(**_DET))
        _judged("r2", _sc(**_UNSEEDED))
        _judged("r3", _sc(evaluated=False))
        assert "judge determinism: deterministic (temp=0.0, seed=42)" in _out("check", "r1")[1]
        assert "judge determinism: non-deterministic" in _out("check", "r2")[1]
        assert "judge determinism" not in _out("check", "r3")[1]
        for run_id, expected in (
            ("r1", "deterministic"),
            ("r2", "non-deterministic"),
            ("r3", None),
        ):
            payload = json.loads(
                CliRunner().invoke(app, ["check", run_id, "--format", "json"]).output
            )
            assert payload["judge_determinism"] == expected

    def test_diff_warns_when_the_judge_configs_differ(self):
        _judged("a", _sc(**_DET))
        _judged("b", reviewed=[{**_DET, "seed": 7}])
        code, out = _out("diff", "a", "b")
        assert code == 0
        assert "verdicts not comparable: judge configs differ" in out
        assert "before temp=0.0, seed=42; after temp=0.0, seed=7" in out

    def test_diff_is_quiet_when_the_configs_match_or_one_is_unrecorded(self):
        _judged("a", _sc(**_DET), reviewed=[_DET])
        _judged("b", _sc(**_DET))
        _judged("old", _sc())
        _judged("none")
        for other in ("b", "old", "none"):
            code, out = _out("diff", "a", other)
            assert code == 0 and "not comparable" not in out

    def test_diff_warns_when_only_the_temperature_differs(self):
        _judged("a", _sc(**_DET))
        _judged("b", _sc(deterministic=False, temperature=0.5, seed=42))
        code, out = _out("diff", "a", "b")
        assert code == 0
        assert "before temp=0.0, seed=42; after temp=0.5, seed=42" in out

    def test_a_positional_arguscfg_call_still_means_what_it_meant(self):
        """The sampling fields are last, so older positional callers do not shift."""
        config = ArgusConfig(
            50_000, False, True, None, None, False, True, True, True, "gpt-4o", "skip"
        )
        assert config.on_judge_failure == "skip"
        assert (config.judge_temperature, config.judge_seed) == (None, None)

    def test_a_non_deterministic_verdict_moves_no_gate(self):
        """`deterministic` is never read by the gate, the exit code, or the status."""
        det = _judged("det", _sc(**_DET), reviewed=[_DET])
        loose = _judged("loose", _sc(**_UNSEEDED), reviewed=[_UNSEEDED])
        assert evaluate_run(det).passed and evaluate_run(loose).passed
        assert evaluate_run(det) == dataclasses.replace(evaluate_run(loose), run_id="det")
        assert det.overall_status == loose.overall_status == "clean"
        assert _out("check", "det")[0] == _out("check", "loose")[0] == 0
        _judged("det_bad", _sc(**_DET), status="crashed")
        _judged("loose_bad", _sc(**_UNSEEDED), status="crashed")
        assert _out("check", "det_bad")[0] == _out("check", "loose_bad")[0] == 1
        assert _out("diff", "det", "loose")[0] == 0

    def test_a_mixed_run_gates_like_either_kind(self):
        mixed = _judged("mix", _sc(**_DET), reviewed=[_UNSEEDED])
        assert determinism.status(mixed) == "mixed"
        assert evaluate_run(mixed).passed and _out("check", "mix")[0] == 0
        crashed = _judged("mix_bad", _sc(**_DET), reviewed=[_UNSEEDED], status="crashed")
        assert not evaluate_run(crashed).passed and _out("check", "mix_bad")[0] == 1


# ═════════════════════════════════════════════════════════════════════════════
# Reading the sampling back from a hand-editable run file
# ═════════════════════════════════════════════════════════════════════════════


def _from_json(**fields):
    """A stored verdict dict as `json.loads` makes it: every value is given as JSON text."""
    return determinism.stamp_from_record(
        json.loads("{" + ", ".join(f'"{k}": {v}' for k, v in fields.items()) + "}")
    )


@pytest.mark.unit
class TestLoadingStoredSampling:
    @pytest.mark.parametrize(
        "raw",
        ["1" + "0" * 400, "NaN", "Infinity", "-Infinity", "-0.5", "2.5", "true", '"hot"', "[]"],
        ids=lambda raw: "huge-int" if len(raw) > 20 else raw,
    )
    def test_a_temperature_that_is_not_a_real_in_range_number_loads_as_not_recorded(self, raw):
        stamped = _from_json(deterministic="true", temperature=raw, seed=3)
        assert (stamped["deterministic"], stamped["temperature"]) == (False, None)

    @pytest.mark.parametrize("raw", ["0", "0.0", "0.7", "1", "2", "2.0"])
    def test_a_real_in_range_temperature_is_kept(self, raw):
        assert _from_json(temperature=raw)["temperature"] == json.loads(raw)

    @pytest.mark.parametrize(
        "raw",
        [str(2**63), str(-(2**63) - 1), "1" + "0" * 400, "true", '"7"', "1.5", "[]"],
        ids=lambda raw: "huge-int" if len(raw) > 20 else raw,
    )
    def test_a_seed_that_is_not_a_64_bit_integer_loads_as_no_seed(self, raw):
        assert _from_json(temperature="0.0", seed=raw)["seed"] is None

    @pytest.mark.parametrize("seed", [0, 7, -(2**63), 2**63 - 1])
    def test_a_64_bit_seed_is_kept(self, seed):
        assert _from_json(temperature="0.0", seed=str(seed))["seed"] == seed

    @pytest.mark.parametrize(
        "fields",
        [
            {"temperature": "0.7", "seed": "3"},  # not at temperature 0.0
            {"temperature": "0.0", "seed": "null"},  # no seed
            {"temperature": "0.0"},  # seed missing
            {"seed": "3"},  # temperature missing
            {},  # nothing recorded
            {"temperature": "0.0", "seed": "true"},  # a bool is no seed
        ],
    )
    def test_a_deterministic_flag_without_pinned_sampling_is_not_believed(self, fields):
        assert _from_json(deterministic="true", **fields)["deterministic"] is False

    @pytest.mark.parametrize("temperature", ["0", "0.0"])
    @pytest.mark.parametrize("seed", ["0", "3"])
    def test_a_pinned_record_stays_deterministic(self, temperature, seed):
        stamped = _from_json(deterministic="true", temperature=temperature, seed=seed)
        assert stamped["deterministic"] is True
        assert (stamped["temperature"], stamped["seed"]) == (0.0, int(seed))

    def test_the_flag_still_has_to_be_a_true(self):
        assert (
            _from_json(deterministic='"yes"', temperature="0.0", seed="3")["deterministic"]
            is False
        )
        assert _from_json(deterministic="1", temperature="0.0", seed="3")["deterministic"] is False

    JUNK = [
        {"deterministic": True, "temperature": 10**400, "seed": 7},
        {"deterministic": True, "temperature": float("nan"), "seed": 7},
        {"deterministic": True, "temperature": float("inf"), "seed": 7},
        {"deterministic": True, "temperature": -1.0, "seed": 7},
        {"deterministic": True, "temperature": True, "seed": 7},
        {"deterministic": True, "temperature": "hot", "seed": 7},
        {"deterministic": True, "temperature": 0.0, "seed": 10**400},
        {"deterministic": True, "temperature": 0.7, "seed": 3},
        {"deterministic": True, "temperature": 0.0, "seed": None},
    ]

    @pytest.mark.parametrize(
        "junk", JUNK, ids=lambda j: f"temp={str(j['temperature'])[:6]}-seed={str(j['seed'])[:6]}"
    )
    def test_show_and_check_survive_junk_sampling_and_call_it_non_deterministic(
        self, tmp_path, junk
    ):
        _judged("r1", _sc(**_DET), reviewed=[_DET])
        path = tmp_path / ".argus" / "runs" / "r1.json"
        data = json.loads(path.read_text("utf-8"))
        data["steps"][0]["semantic_check"].update(junk)
        data["steps"][1]["review"][0].update(junk)
        path.write_text(json.dumps(data), "utf-8")
        record = load_run("r1")
        assert determinism.status(record) == "non-deterministic"
        assert determinism.describe(record).startswith("non-deterministic (")
        code, out = _out("show", "r1")
        assert code == 0 and "judge determinism: non-deterministic (" in out
        code, out = _out("check", "r1", "--format", "json")
        assert code == 0 and json.loads(out)["judge_determinism"] == "non-deterministic"

    @pytest.mark.parametrize("junk", [None, 7, "text", {"a": 1}], ids=repr)
    def test_a_review_that_is_not_a_list_is_ignored_by_the_reports(self, tmp_path, junk):
        """A run file edited by hand: `check` (the CI gate command) and `diff` must not crash."""
        _judged("r1", _sc(**_DET), reviewed=[_DET])
        _judged("r2", _sc(**_DET))
        path = tmp_path / ".argus" / "runs" / "r1.json"
        data = json.loads(path.read_text("utf-8"))
        data["steps"][1]["review"] = junk
        path.write_text(json.dumps(data), "utf-8")
        assert determinism.status(load_run("r1")) == "deterministic"
        assert _out("check", "r1")[0] == 0
        assert json.loads(_out("check", "r1", "--format", "json")[1])["judge_determinism"] == (
            "deterministic"
        )
        assert _out("diff", "r1", "r2")[0] == 0

    def test_a_review_entry_that_is_not_a_dict_is_ignored(self, tmp_path):
        _judged("r1", _sc(**_DET), reviewed=[_DET])
        path = tmp_path / ".argus" / "runs" / "r1.json"
        data = json.loads(path.read_text("utf-8"))
        data["steps"][1]["review"].append("junk")
        path.write_text(json.dumps(data), "utf-8")
        assert determinism.status(load_run("r1")) == "deterministic"

    def test_show_and_check_still_call_a_pinned_record_deterministic(self, tmp_path):
        _judged("r1", _sc(**_DET), reviewed=[_DET])
        path = tmp_path / ".argus" / "runs" / "r1.json"
        data = json.loads(path.read_text("utf-8"))
        data["steps"][0]["semantic_check"].update(temperature=0, seed=3)
        data["steps"][1]["review"][0].update(temperature=0, seed=3)
        path.write_text(json.dumps(data), "utf-8")
        assert determinism.describe(load_run("r1")).startswith("deterministic (temp=0.0, seed=")
        assert "judge determinism: deterministic (" in _out("show", "r1")[1]
        assert json.loads(_out("check", "r1", "--format", "json")[1])["judge_determinism"] == (
            "deterministic"
        )


@pytest.mark.unit
class TestCombine:
    def test_every_call_must_have_been_deterministic(self):
        det = determinism.stamp_from_record({**_DET, "system_fingerprint": "fp_b"})
        loose = determinism.stamp_from_record(_UNSEEDED)
        assert determinism.combine([det, det])["deterministic"] is True
        assert determinism.combine([det, loose])["deterministic"] is False

    def test_the_sampling_is_the_first_calls_and_the_fingerprints_are_all_of_them(self):
        a = determinism.stamp_from_record({**_DET, "system_fingerprint": "fp_b"})
        b = determinism.stamp_from_record({**_DET, "system_fingerprint": "fp_a"})
        combined = determinism.combine([a, b, a])
        assert (combined["temperature"], combined["seed"]) == (0.0, 42)
        assert combined["system_fingerprint"] == "fp_a,fp_b"

    def test_no_calls_is_the_not_recorded_default(self):
        assert determinism.combine([]) == determinism.stamp_from_record({})
