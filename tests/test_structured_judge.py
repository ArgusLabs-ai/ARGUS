"""Structured judge protocol: forced tool-call verdicts + custom check specs.

No network anywhere — the LLM proxy and the provider HTTP layer are mocked
the same way as tests/test_semantic_checker_unit.py and tests/test_smoke.py.
"""

from __future__ import annotations

import json

import pytest

from argus.models import CheckVerdict, SemanticSignal
from argus.semantic_checker import (
    JUDGE_TOOL_CHOICE,
    JUDGE_VERDICT_TOOL,
    _parse_verdict_payload,
    _verdict_from_tool_calls,
    build_verdict_tool,
    check_semantic_coherence,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _tool_call_result(arguments: dict | str, content: str = ""):
    """A normalized completion whose verdict arrived as a forced tool call."""
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "record_verdict", "arguments": arguments},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _mock_llm(monkeypatch, response, captured=None):
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)

    def fake_llm(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return response

    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", fake_llm)


@pytest.mark.unit
class TestToolSchemaBuild:
    def test_judge_tool_schema_shape(self):
        fn = JUDGE_VERDICT_TOOL
        assert fn["type"] == "function"
        assert fn["function"]["name"] == "record_verdict"
        params = fn["function"]["parameters"]
        assert params["type"] == "object"
        for key in (
            "pass",
            "reason",
            "confidence",
            "failure_kind",
            "evidence_considered",
            "overridden_signals",
            "disambiguation_verdicts",
        ):
            assert key in params["properties"]
        for key in ("pass", "reason", "confidence", "failure_kind"):
            assert key in params["required"]
        assert params["properties"]["failure_kind"]["enum"] == [
            "contradiction",
            "empty_or_missing",
            "other",
            "unrelated",
        ]
        dis = params["properties"]["disambiguation_verdicts"]["items"]
        assert set(dis["required"]) == {"sig_id", "is_failure", "confidence", "reason"}

    def test_tool_choice_forces_the_verdict_function(self):
        assert JUDGE_TOOL_CHOICE == {
            "type": "function",
            "function": {"name": "record_verdict"},
        }

    def test_builder_mirrors_arguments(self):
        tool = build_verdict_tool(
            "my_tool", "desc", {"x": {"type": "boolean"}}, ["x"]
        )
        assert tool["function"]["name"] == "my_tool"
        assert tool["function"]["parameters"]["properties"] == {"x": {"type": "boolean"}}
        assert tool["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.unit
class TestVerdictFromToolCalls:
    def test_parses_tool_call_arguments(self):
        parsed = _verdict_from_tool_calls(
            _tool_call_result({"pass": True, "reason": "ok", "confidence": 0.9})
        )
        assert parsed == {"pass": True, "reason": "ok", "confidence": 0.9}

    def test_unparseable_arguments_are_no_verdict(self):
        assert (
            _verdict_from_tool_calls(_tool_call_result('{"pass": tru')) is None
        )

    def test_truncated_arguments_are_repaired(self):
        """max_tokens cut the forced call mid-string; the old content path salvaged this."""
        parsed = _verdict_from_tool_calls(
            _tool_call_result('{"pass": false, "confidence": 0.9, "reason": "the summary contradi')
        )
        assert parsed == {"pass": False, "confidence": 0.9, "reason": "the summary contradi"}

    def test_no_tool_calls_is_no_verdict(self):
        assert (
            _verdict_from_tool_calls(
                {"choices": [{"message": {"content": '{"pass": true}'}}]}
            )
            is None
        )

    def test_empty_choices_is_no_verdict(self):
        assert _verdict_from_tool_calls({"choices": []}) is None

    def test_dict_arguments_tolerated(self):
        result = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c",
                                "type": "function",
                                "function": {
                                    "name": "record_verdict",
                                    "arguments": {"pass": False},
                                },
                            }
                        ],
                    }
                }
            ]
        }
        assert _verdict_from_tool_calls(result) == {"pass": False}

    def test_payload_parser_prefers_tool_calls(self):
        result = _tool_call_result(
            {"pass": False, "reason": "from tool"},
            content='{"pass": true, "reason": "from prose"}',
        )
        assert _parse_verdict_payload(result)["reason"] == "from tool"

    def test_payload_parser_falls_back_to_content(self):
        result = {
            "choices": [{"message": {"content": '{"pass": true, "confidence": 0.8}'}}],
            "usage": {},
        }
        assert _parse_verdict_payload(result)["confidence"] == 0.8


@pytest.mark.unit
class TestStructuredCoherenceJudge:
    def test_verdict_read_from_tool_call(self, monkeypatch):
        _mock_llm(
            monkeypatch,
            _tool_call_result(
                {
                    "pass": False,
                    "reason": "unrelated output",
                    "confidence": 0.9,
                    "failure_kind": "unrelated",
                    "evidence_considered": ["PH-001"],
                    "overridden_signals": [],
                    "disambiguation_verdicts": [],
                }
            ),
        )
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.evaluated is True
        assert result.passed is False
        assert result.failure_kind == "unrelated"
        assert result.evidence_considered == ("PH-001",)

    def test_tools_and_tool_choice_passed_to_proxy(self, monkeypatch):
        captured: dict = {}
        _mock_llm(monkeypatch, _tool_call_result({"pass": True}), captured=captured)
        check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert captured["tools"] == [JUDGE_VERDICT_TOOL]
        assert captured["tool_choice"] == JUDGE_TOOL_CHOICE

    def test_unparseable_tool_args_fall_back_to_content_once(self, monkeypatch):
        """Unparseable arguments + parseable content → content verdict wins."""
        response = _tool_call_result(
            '{"pass": tru', content='{"pass": true, "confidence": 0.7}'
        )
        calls = []

        def counting_llm(**kwargs):
            calls.append(kwargs)
            return response

        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", counting_llm)

        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert len(calls) == 1, "fallback must not make a second LLM call"
        assert result.evaluated is True
        assert result.passed is True

    def test_truncated_tool_arguments_still_yield_a_verdict(self, monkeypatch):
        """No content to fall back to: the repaired arguments are the verdict."""
        _mock_llm(
            monkeypatch,
            _tool_call_result('{"pass": false, "confidence": 0.9, "reason": "unrelated outp'),
        )
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.evaluated is True
        assert result.passed is False
        assert result.reason == "unrelated outp"

    def test_unparseable_everywhere_is_a_skip(self, monkeypatch):
        response = _tool_call_result("not json", content="also not json")
        _mock_llm(monkeypatch, response)
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.evaluated is False
        assert result.passed is True

    def test_content_only_response_still_evaluates(self, monkeypatch):
        """Providers/mocks that ignore tools keep working via the free-text path."""
        _mock_llm(
            monkeypatch,
            {
                "choices": [{"message": {"content": json.dumps({"pass": True})}}],
                "usage": {},
            },
        )
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.evaluated is True

    def test_disambiguation_from_tool_call(self, monkeypatch):
        signal = SemanticSignal(
            sig_id="PH-001",
            category="placeholder_outputs",
            severity="warning",
            description="looks like placeholder",
            field_path=("result",),
            evidence="TODO",
            confidence=0.5,
        )
        _mock_llm(
            monkeypatch,
            _tool_call_result(
                {
                    "pass": True,
                    "reason": "ok",
                    "confidence": 0.9,
                    "failure_kind": "other",
                    "evidence_considered": [],
                    "overridden_signals": [],
                    "disambiguation_verdicts": [
                        {
                            "sig_id": "PH-001",
                            "is_failure": False,
                            "confidence": 0.8,
                            "reason": "legitimate TODO marker",
                        }
                    ],
                }
            ),
        )
        _, dis = check_semantic_coherence(
            "node_a", {"q": "hello"}, {"a": "world"}, ambiguous_signals=[signal]
        )
        assert len(dis) == 1
        assert dis[0].llm_verdict is False


@pytest.mark.unit
class TestOpenAIToolsPassthrough:
    def test_tools_reach_the_openai_payload(self, monkeypatch):
        import argus.providers as providers

        captured: dict = {}

        class _FakeResp:
            def __init__(self, payload):
                self._data = json.dumps(payload).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return _FakeResp(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "record_verdict",
                                            "arguments": '{"pass": true}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }
            )

        monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

        out = providers.call_openai(
            api_key="sk-test",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            tools=[JUDGE_VERDICT_TOOL],
            tool_choice=JUDGE_TOOL_CHOICE,
        )
        body = captured["body"]
        assert body["tools"] == [JUDGE_VERDICT_TOOL]
        assert body["tool_choice"] == JUDGE_TOOL_CHOICE
        # response round-trips in the OpenAI shape with tool_calls attached
        assert out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == (
            '{"pass": true}'
        )

    def test_tools_omitted_when_not_requested(self, monkeypatch):
        import argus.providers as providers

        captured: dict = {}

        class _FakeResp:
            def __init__(self, payload):
                self._data = json.dumps(payload).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

        monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
        providers.call_openai(
            api_key="sk-test",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert "tools" not in captured["body"]
        assert "tool_choice" not in captured["body"]

    def test_anthropic_tool_translation(self, monkeypatch):
        import argus.providers as providers

        captured: dict = {}

        class _FakeResp:
            def __init__(self, payload):
                self._data = json.dumps(payload).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "record_verdict",
                            "input": {"pass": True, "reason": "fine"},
                        }
                    ],
                    "usage": {"input_tokens": 9, "output_tokens": 4},
                }
            )

        monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
        out = providers.call_anthropic(
            api_key="sk-ant",
            model="claude-3-5-haiku-latest",
            messages=[{"role": "user", "content": "hi"}],
            tools=[JUDGE_VERDICT_TOOL],
            tool_choice=JUDGE_TOOL_CHOICE,
        )
        body = captured["body"]
        assert body["tools"][0]["name"] == "record_verdict"
        assert body["tools"][0]["input_schema"]["type"] == "object"
        assert body["tool_choice"] == {"type": "tool", "name": "record_verdict"}
        tc = out["choices"][0]["message"]["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "record_verdict"
        assert json.loads(tc["function"]["arguments"]) == {"pass": True, "reason": "fine"}
        assert out["usage"]["prompt_tokens"] == 9

    def test_google_tool_translation(self, monkeypatch):
        import argus.providers as providers

        captured: dict = {}

        class _FakeResp:
            def __init__(self, payload):
                self._data = json.dumps(payload).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeResp(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "record_verdict",
                                            "args": {"pass": False, "reason": "no"},
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
                }
            )

        from argus.judge_specs import build_check_tool

        monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
        out = providers.call_google(
            api_key="g-key",
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            tools=[JUDGE_VERDICT_TOOL, build_check_tool(_spec())],
            tool_choice=JUDGE_TOOL_CHOICE,
        )
        body = captured["body"]
        decl = body["tools"][0]["functionDeclarations"][0]
        assert decl["name"] == "record_verdict"
        assert decl["parameters"]["type"] == "OBJECT"
        # Gemini's FunctionDeclaration schema rejects additionalProperties; the
        # tools we build emit it at the top level and inside array items.
        assert "additionalProperties" in json.dumps(JUDGE_VERDICT_TOOL)
        assert "additionalProperties" in json.dumps(build_check_tool(_spec()))
        assert len(body["tools"][0]["functionDeclarations"]) == 2
        assert "additionalProperties" not in json.dumps(body)
        assert body["toolConfig"]["functionCallingConfig"] == {
            "mode": "ANY",
            "allowedFunctionNames": ["record_verdict"],
        }
        tc = out["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "record_verdict"
        assert json.loads(tc["function"]["arguments"]) == {"pass": False, "reason": "no"}


@pytest.mark.unit
class TestCheckSpecLoading:
    def _write(self, tmp_path, name, payload):
        d = tmp_path / ".argus" / "checks"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
        return d

    def test_missing_dir_is_a_no_op(self, tmp_path):
        from argus.judge_specs import load_check_specs

        assert load_check_specs(tmp_path / "nope") == []

    def test_valid_spec_loads(self, tmp_path):
        from argus.judge_specs import load_check_specs

        d = self._write(
            tmp_path,
            "pii.json",
            {
                "name": "no_pii",
                "prompt": "Does {{node}} output leak PII? {{output}}",
                "choices": {
                    "clean": {"score": 1.0, "label": "no PII"},
                    "leak": {"score": 0.0, "label": "leaks PII"},
                },
                "severity": "warning",
                "when": "any",
            },
        )
        specs = load_check_specs(d)
        assert len(specs) == 1
        assert specs[0].name == "no_pii"
        assert specs[0].choices["leak"]["score"] == 0.0
        assert specs[0].severity == "warning"
        assert specs[0].when == "any"

    def test_defaults(self, tmp_path):
        from argus.judge_specs import load_check_specs

        d = self._write(tmp_path, "minimal.json", {"name": "x", "prompt": "ok {{node}}"})
        (spec,) = load_check_specs(d)
        assert spec.severity == "warning"
        assert spec.when == "any"
        assert spec.choices == {}

    def test_invalid_spec_is_skipped_not_fatal(self, tmp_path):
        from argus.judge_specs import load_check_specs

        d = self._write(tmp_path, "bad.json", {"name": "", "prompt": "x"})
        self._write(tmp_path, "also_bad.json", "{not json")
        self._write(
            tmp_path,
            "bad_severity.json",
            {"name": "x", "prompt": "p", "severity": "fatal"},
        )
        self._write(
            tmp_path,
            "bad_choice.json",
            {"name": "x", "prompt": "p", "choices": {"A": {"score": 7}}},
        )
        self._write(tmp_path, "good.json", {"name": "ok", "prompt": "p"})
        specs = load_check_specs(d)
        assert [s.name for s in specs] == ["ok"]

    def test_duplicate_name_is_rejected_first_wins(self, tmp_path, caplog):
        """Two specs named alike would both emit ``check:<name>``; keep the first."""
        import logging

        from argus.judge_specs import load_check_specs

        d = self._write(tmp_path, "a_first.json", {"name": "dup", "prompt": "first"})
        self._write(tmp_path, "b_second.json", {"name": " dup ", "prompt": "second"})
        self._write(tmp_path, "c_other.json", {"name": "other", "prompt": "third"})
        with caplog.at_level(logging.WARNING, logger="argus"):
            specs = load_check_specs(d)
        assert [(s.name, s.prompt) for s in specs] == [("dup", "first"), ("other", "third")]
        (record,) = [r for r in caplog.records if "duplicate" in r.getMessage()]
        assert "b_second.json" in record.getMessage()
        assert "dup" in record.getMessage()

    def test_non_object_top_level_skipped(self, tmp_path):
        from argus.judge_specs import load_check_specs

        d = self._write(tmp_path, "list.json", "[1, 2]")
        assert load_check_specs(d) == []


@pytest.mark.unit
class TestPlaceholderSubstitution:
    def test_renders_all_three_placeholders(self):
        from argus.judge_specs import substitute_placeholders

        out = substitute_placeholders(
            "node={{node}} in={{input}} out={{output}}",
            node_name="summarize",
            input_state={"q": "hello"},
            output_dict={"a": "world"},
        )
        assert "node=summarize" in out
        assert '"q": "hello"' in out
        assert '"a": "world"' in out

    def test_unknown_placeholder_left_alone(self):
        from argus.judge_specs import substitute_placeholders

        out = substitute_placeholders(
            "{{node}} {{bogus}}", node_name="n", input_state={}, output_dict={}
        )
        assert out == "n {{bogus}}"

    def test_whitespace_tolerated(self):
        from argus.judge_specs import substitute_placeholders

        out = substitute_placeholders(
            "{{ node }}", node_name="n", input_state={}, output_dict={}
        )
        assert out == "n"


def _spec(**overrides):
    from pathlib import Path

    from argus.judge_specs import CheckSpec

    base = {
        "name": "no_pii",
        "prompt": "Does the output leak PII? {{output}}",
        "choices": {
            "clean": {"score": 1.0, "label": "no PII"},
            "leak": {"score": 0.0, "label": "leak"},
        },
        "severity": "warning",
        "when": "any",
        "path": Path("x.json"),
    }
    base.update(overrides)
    return CheckSpec(**base)


def _check_tool_result(arguments: dict):
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "record_check_verdict",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 6},
    }


@pytest.mark.unit
class TestRunCheckSpec:
    def test_graded_choice_verdict(self, monkeypatch):
        from argus.judge_specs import build_check_tool, run_check_spec

        captured: dict = {}
        _mock_llm(
            monkeypatch,
            _check_tool_result({"verdict": "leak", "reason": "found an SSN"}),
            captured=captured,
        )
        spec = _spec()
        verdict = run_check_spec(spec, "node_a", {"q": "x"}, {"a": "ssn 123"}, model="m")
        assert isinstance(verdict, CheckVerdict)
        assert verdict.evaluated is True
        assert verdict.verdict == "leak"
        assert verdict.score == 0.0
        assert verdict.passed is False
        assert verdict.reason == "found an SSN"
        assert verdict.prompt_tokens == 20
        # forced tool-call channel, same builder family as the judge
        assert captured["tools"] == [build_check_tool(spec)]
        assert captured["tool_choice"] == {
            "type": "function",
            "function": {"name": "record_check_verdict"},
        }
        # placeholder was substituted in the outgoing prompt
        assert "ssn 123" in captured["messages"][1]["content"]

    def test_pass_fail_style_spec(self, monkeypatch):
        from pathlib import Path

        from argus.judge_specs import CheckSpec, run_check_spec

        spec = CheckSpec(
            name="on_topic",
            prompt="Is it on topic? {{input}} {{output}}",
            choices={},
            severity="info",
            when="any",
            path=Path("x.json"),
        )
        _mock_llm(monkeypatch, _check_tool_result({"pass": False, "reason": "off topic"}))
        verdict = run_check_spec(spec, "n", {"q": "cats"}, {"a": "dogs"}, model="m")
        assert verdict.evaluated is True
        assert verdict.verdict == "fail"
        assert verdict.score is None
        assert verdict.passed is False
        assert verdict.severity == "info"

    def test_unknown_choice_is_unevaluated(self, monkeypatch):
        _mock_llm(monkeypatch, _check_tool_result({"verdict": "maybe", "reason": "?"})
        )
        from argus.judge_specs import run_check_spec

        verdict = run_check_spec(_spec(), "n", {"q": "x"}, {"a": "y"}, model="m")
        assert verdict.evaluated is False
        assert verdict.passed is True  # skips stay non-blocking

    def test_unparseable_response_is_unevaluated(self, monkeypatch):
        _mock_llm(monkeypatch, {"choices": [{"message": {"content": "garbage"}}], "usage": {}})
        from argus.judge_specs import run_check_spec

        verdict = run_check_spec(_spec(), "n", {"q": "x"}, {"a": "y"}, model="m")
        assert verdict.evaluated is False

    def test_not_available_is_unevaluated(self, monkeypatch):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: False)
        from argus.judge_specs import run_check_spec

        verdict = run_check_spec(_spec(), "n", {"q": "x"}, {"a": "y"}, model="m")
        assert verdict.evaluated is False


@pytest.mark.unit
class TestSessionCustomCheckWiring:
    """Spec verdict -> SemanticSignal via ArgusSession._run_custom_check_specs."""

    def _session(self, monkeypatch, specs):
        from argus.session import ArgusSession

        session = ArgusSession()
        monkeypatch.setattr(session, "_get_check_specs", lambda: specs)
        return session

    def test_verdict_becomes_semantic_signal(self, monkeypatch, tmp_path):
        _mock_llm(monkeypatch, _check_tool_result({"verdict": "leak", "reason": "SSN found"}))
        session = self._session(monkeypatch, [_spec()])

        (sig,) = session._run_custom_check_specs("node_a", {"q": "x"}, {"a": "ssn"}, False)

        assert sig.sig_id == "check:no_pii"
        assert sig.category == "custom_check"
        assert sig.severity == "warning"  # spec severity when not passed
        assert sig.evidence == "leak"
        assert sig.confidence == 0.0  # the choice's score
        assert sig.description == "SSN found"

    def test_passing_check_attaches_info_signal(self, monkeypatch, tmp_path):
        _mock_llm(monkeypatch, _check_tool_result({"verdict": "clean", "reason": "fine"}))
        session = self._session(monkeypatch, [_spec()])

        (sig,) = session._run_custom_check_specs("node_a", {"q": "x"}, {"a": "clean"}, False)
        assert sig.severity == "info"
        assert sig.confidence == 1.0

    def test_unevaluated_verdict_attaches_nothing(self, monkeypatch, tmp_path):
        _mock_llm(monkeypatch, {"choices": [{"message": {"content": "garbage"}}], "usage": {}})
        session = self._session(monkeypatch, [_spec()])

        assert session._run_custom_check_specs("node_a", {"q": "x"}, {"a": "y"}, False) == []

    def test_when_warning_signal_requires_signals(self, monkeypatch, tmp_path):
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr(
            "argus.llm_proxy.create_chat_completion",
            lambda **kwargs: _check_tool_result({"verdict": "clean", "reason": "ok"}),
        )
        session = self._session(
            monkeypatch,
            [_spec(name="only_with_flags", when="warning_signal"), _spec(name="always")],
        )

        # no heuristic signals on the step: the warning_signal spec must not run
        signals = session._run_custom_check_specs("node_a", {"q": "x"}, {"a": "y"}, False)
        assert [s.sig_id for s in signals] == ["check:always"]

        signals = session._run_custom_check_specs("node_a", {"q": "x"}, {"a": "y"}, True)
        assert [s.sig_id for s in signals] == ["check:only_with_flags", "check:always"]

    def test_broken_spec_never_breaks_the_judge(self, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("spec exploded")

        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", boom)
        session = self._session(monkeypatch, [_spec()])

        assert session._run_custom_check_specs("node_a", {"q": "x"}, {"a": "y"}, False) == []


_CLEAN_OUT = {"summary": "Q3 revenue grew 12%."}
# PH-001, a warning-level signature: the coherence judge is asked about it.
_TODO_OUT = {"summary": "Q3 revenue grew 12%.", "notes": "TODO"}
# SP-002 at confidence 0.58: inside the ambiguous band, so the judge may dismiss it.
_AMBIGUOUS_OUT = {"summary": "Q3 revenue grew 12%.", "notes": "sorry I cannot do that"}

_PII_SPEC = {
    "name": "no_pii",
    "prompt": "Does the output leak PII? {{output}}",
    "choices": {"clean": {"score": 1.0}, "leak": {"score": 0.0}},
    "severity": "warning",
    "when": "any",
}
_LEAK = {"verdict": "leak", "reason": "SSN found"}


def _install_llm(monkeypatch, *, check, coherence=None):
    """Fake the transport: answers by which verdict tool the call forced."""
    calls: dict[str, list] = {"check": [], "coherence": []}
    monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)

    def fake(**kwargs):
        if kwargs["tools"][0]["function"]["name"] == "record_check_verdict":
            calls["check"].append(kwargs)
            return _check_tool_result(check)
        calls["coherence"].append(kwargs)
        return _tool_call_result(
            coherence or {"pass": True, "reason": "fine", "confidence": 0.9}
        )

    monkeypatch.setattr("argus.llm_proxy.create_chat_completion", fake)
    return calls


def _run_step(
    tmp_path,
    *,
    output,
    specs=(_PII_SPEC,),
    semantic_check=True,
    enabled=True,
    on_judge_failure="warn",
):
    """One node through a real ArgusSession (wrap -> on_node_end -> finalize)."""
    from argus.models import ArgusConfig, LLMInvestigationConfig
    from argus.session import ArgusSession
    from argus.storage import load_run

    checks = tmp_path / ".argus" / "checks"
    checks.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        (checks / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")
    session = ArgusSession(
        llm_investigation=LLMInvestigationConfig(enabled=enabled, semantic_check=semantic_check),
        config=ArgusConfig(on_judge_failure=on_judge_failure),
    )
    wrapped = session.wrap("summarize", lambda state: dict(output))
    session.set_node_names(["summarize"])
    session.set_edges({})
    wrapped({"query": "q"})
    session.finalize()
    record = load_run(session.run_id)
    return record, record.steps[0]


def _sig_ids(step):
    return [s.sig_id for s in step.inspection.semantic_signals]


_BOTH_JUDGE_PATHS = pytest.mark.parametrize(
    "on_judge_failure",
    [
        pytest.param("warn", id="deferred"),
        pytest.param("abort", id="sync"),
    ],
)


@pytest.mark.integration
class TestCustomChecksThroughTheSession:
    """Custom checks run on their own path and stay advisory."""

    @_BOTH_JUDGE_PATHS
    def test_when_any_check_runs_on_a_clean_step(self, monkeypatch, tmp_path, on_judge_failure):
        calls = _install_llm(monkeypatch, check={"verdict": "clean", "reason": "no PII"})
        _, step = _run_step(tmp_path, output=_CLEAN_OUT, on_judge_failure=on_judge_failure)

        assert len(calls["check"]) == 1, "once per step per spec, never twice"
        assert calls["coherence"] == [], "a clean step is not the coherence judge's business"
        assert _sig_ids(step) == ["check:no_pii"]
        assert step.status == "pass"

    @_BOTH_JUDGE_PATHS
    def test_custom_check_runs_with_semantic_check_off(
        self, monkeypatch, tmp_path, on_judge_failure
    ):
        calls = _install_llm(monkeypatch, check=_LEAK)
        _, step = _run_step(
            tmp_path, output=_TODO_OUT, semantic_check=False, on_judge_failure=on_judge_failure
        )

        assert len(calls["check"]) == 1
        assert calls["coherence"] == []
        assert "check:no_pii" in _sig_ids(step)

    def test_custom_check_needs_llm_investigation_enabled(self, monkeypatch, tmp_path):
        calls = _install_llm(monkeypatch, check=_LEAK)
        _, step = _run_step(tmp_path, output=_CLEAN_OUT, enabled=False)

        assert calls["check"] == []
        assert step.inspection.semantic_signals == []

    def test_warning_signal_spec_follows_its_own_gate(self, monkeypatch, tmp_path):
        spec = {**_PII_SPEC, "when": "warning_signal"}
        calls = _install_llm(monkeypatch, check=_LEAK)

        _run_step(tmp_path, output=_CLEAN_OUT, specs=[spec], semantic_check=False)
        assert calls["check"] == [], "no heuristic signal on the step"

        _run_step(tmp_path, output=_TODO_OUT, specs=[spec], semantic_check=False)
        assert len(calls["check"]) == 1

    @_BOTH_JUDGE_PATHS
    def test_failing_critical_check_cannot_move_status(
        self, monkeypatch, tmp_path, on_judge_failure
    ):
        from argus.check import evaluate_run

        calls = _install_llm(
            monkeypatch,
            check=_LEAK,
            coherence={
                "pass": True,
                "reason": "legitimate wording",
                # Below the 0.7 override bar: the judge dismisses the flag but
                # does not get to re-pass the step, so status is whatever the
                # dismissal bookkeeping leaves behind.
                "confidence": 0.5,
                "failure_kind": "other",
                "disambiguation_verdicts": [
                    {
                        "sig_id": "SP-002",
                        "is_failure": False,
                        "confidence": 0.8,
                        "reason": "not a refusal",
                    }
                ],
            },
        )
        record, step = _run_step(
            tmp_path,
            output=_AMBIGUOUS_OUT,
            specs=[{**_PII_SPEC, "severity": "critical"}],
            on_judge_failure=on_judge_failure,
        )

        assert len(calls["coherence"]) == 1, "the judge really reviewed the soft flag"
        assert "SP-002" not in _sig_ids(step), "and dismissed it"
        custom = next(s for s in step.inspection.semantic_signals if s.sig_id == "check:no_pii")
        assert custom.severity == "critical", "the finding is still reported"
        assert step.status == "pass"
        assert record.overall_status == "clean"
        assert evaluate_run(record).passed is True

    @_BOTH_JUDGE_PATHS
    def test_coherence_pass_does_not_erase_a_custom_warning(
        self, monkeypatch, tmp_path, on_judge_failure
    ):
        calls = _install_llm(
            monkeypatch,
            check=_LEAK,
            coherence={"pass": True, "reason": "TODO is fine", "confidence": 0.9},
        )
        _, step = _run_step(tmp_path, output=_TODO_OUT, on_judge_failure=on_judge_failure)

        assert len(calls["coherence"]) == 1
        ids = _sig_ids(step)
        assert "PH-001" not in ids, "the judge dismissed the heuristic warning"
        assert "check:no_pii" in ids, "but it never reviewed the custom check"


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.unit
class TestCustomCheckOverHostedProxy:
    """The hosted proxy speaks plain chat: no tools, so the prompt is the contract."""

    def _use_proxy(self, monkeypatch, tmp_path, reply_content):
        import types

        import argus.llm_proxy as llm_proxy

        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("argus.user_config._CONFIG_FILE", tmp_path / "no-config.json")
        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr(llm_proxy, "SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setattr(
            llm_proxy, "_PROXY_URL", "https://example.supabase.co/functions/v1/llm-proxy"
        )
        monkeypatch.setattr(
            llm_proxy, "_get_valid_credentials", lambda: types.SimpleNamespace(access_token="tok")
        )
        sent: dict = {}

        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["body"] = json.loads(req.data)
            return _FakeHTTPResponse(
                {
                    "choices": [{"message": {"content": reply_content}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7},
                }
            )

        monkeypatch.setattr(llm_proxy.urllib.request, "urlopen", fake_urlopen)
        return sent

    @staticmethod
    def _prompt_text(body):
        return "\n".join(m["content"] for m in body["messages"])

    def test_graded_check_reads_a_plain_content_reply(self, monkeypatch, tmp_path):
        from argus.judge_specs import run_check_spec

        sent = self._use_proxy(
            monkeypatch, tmp_path, '{"verdict": "leak", "reason": "found an SSN"}'
        )
        verdict = run_check_spec(_spec(), "node_a", {"q": "x"}, {"a": "ssn 123"}, model="m")

        assert sent["url"].endswith("/llm-proxy"), "the proxy path, not a BYOK adapter"
        assert "tools" not in sent["body"]
        assert "tool_choice" not in sent["body"]
        prompt = self._prompt_text(sent["body"])
        assert '"verdict"' in prompt and '"reason"' in prompt
        assert '"clean"' in prompt and '"leak"' in prompt, "every choice value is spelled out"
        assert verdict.evaluated is True
        assert verdict.verdict == "leak"
        assert verdict.reason == "found an SSN"
        assert verdict.prompt_tokens == 12

    def test_pass_fail_check_reads_a_plain_content_reply(self, monkeypatch, tmp_path):
        from argus.judge_specs import run_check_spec

        sent = self._use_proxy(
            monkeypatch, tmp_path, '```json\n{"pass": false, "reason": "off topic"}\n```'
        )
        verdict = run_check_spec(
            _spec(choices={}, severity="info"), "n", {"q": "cats"}, {"a": "dogs"}, model="m"
        )

        prompt = self._prompt_text(sent["body"])
        assert '"pass"' in prompt and '"reason"' in prompt
        assert '"verdict"' not in prompt
        assert verdict.evaluated is True
        assert verdict.passed is False
