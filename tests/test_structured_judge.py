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
    _extract_json_object,
    _parse_verdict_payload,
    _repair_truncated_json,
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

    def test_verdict_cut_mid_value_is_not_repaired(self):
        """``"clean`` closed by the repair would read as the wrong enum value."""
        reason_first = _tool_call_result('{"reason": "x", "verdict": "clean')
        assert _verdict_from_tool_calls(reason_first) is None
        assert _verdict_from_tool_calls(_tool_call_result('{"verdict": "clean_with_war')) is None
        # a bare number may be the front of a longer one
        assert _verdict_from_tool_calls(_tool_call_result('{"pass": 1')) is None

    def test_a_spelled_out_boolean_verdict_is_kept_when_the_cut_follows_it(self):
        parsed = _verdict_from_tool_calls(_tool_call_result('{"pass": true'))
        assert parsed == {"pass": True}

    def test_a_closed_verdict_string_is_kept_when_the_cut_falls_in_the_reason(self):
        parsed = _verdict_from_tool_calls(
            _tool_call_result('{"verdict": "clean_with_warnings", "reason": "the summary omit')
        )
        assert parsed == {"verdict": "clean_with_warnings", "reason": "the summary omit"}

    def test_a_closed_verdict_string_ending_the_payload_is_kept(self):
        parsed = _verdict_from_tool_calls(_tool_call_result('{"verdict": "leak"'))
        assert parsed == {"verdict": "leak"}

    def test_a_repeated_decisive_key_is_never_repaired(self):
        """The cut ``verdict`` is not the *last* key by position once it repeats.

        ``json.loads`` keeps the first key's slot but the last value, so a
        position check passes ``clean_with_wa`` off as the ruling.
        """
        cut_repeat = '{"verdict": "clean", "reason": "x", "verdict": "clean_with_wa'
        assert _verdict_from_tool_calls(_tool_call_result(cut_repeat)) is None
        # a whole repeat that disagrees is no ruling either
        whole_repeat = '{"pass": true, "reason": "x", "pass": false'
        assert _verdict_from_tool_calls(_tool_call_result(whole_repeat)) is None

    def test_a_decisive_key_named_inside_a_string_is_not_a_repeat(self):
        quoted = '{"verdict": "leak", "reason": "said \\"verdict\\": \\"clean\\" befo'
        parsed = _verdict_from_tool_calls(_tool_call_result(quoted))
        assert parsed == {"verdict": "leak", "reason": 'said "verdict": "clean" befo'}

    def test_cuts_the_repair_cannot_close_stay_unrepaired(self):
        """Not covered, on purpose: no verdict is read from these, as before the guard."""
        after_a_backslash = '{"verdict": "leak", "reason": "a\\'
        in_a_nested_literal = (
            '{"pass": false, "disambiguation_verdicts": [{"sig_id": "PH-001", "is_failure": fal'
        )
        in_a_unicode_escape = '{"pass": false, "reason": "a\\u00'
        in_a_key_name = '{"pass": false, "rea'
        after_the_colon = '{"pass": false, "reason":'
        for cut in (
            after_a_backslash,
            in_a_nested_literal,
            in_a_unicode_escape,
            in_a_key_name,
            after_the_colon,
        ):
            assert _verdict_from_tool_calls(_tool_call_result(cut)) is None, cut

    def test_a_decisive_value_cut_inside_a_nested_object_is_never_repaired(self):
        """A list of per-clause verdicts is read item by item: a cut ``no`` is not ``none``.

        ``"no`` closes to ``no``, which reads as ``False``, while the model was
        writing ``none`` or ``not_applicable``.
        """
        nested = '{"verdicts":[{"clause":"A","cited_steps":[1],"verdict":"no'
        assert _verdict_from_tool_calls(_tool_call_result(nested)) is None
        assert _extract_json_object(nested) is None
        assert _parse_verdict_payload(_tool_call_result(nested)) is None
        for cut in (
            '{"verdicts":[{"verdict":"none"},{"clause":"B","verdict":"not_appl',  # a later item
            '{"outer":{"inner":{"pass":1',  # a bare number may be the front of a longer one
            '{"verdicts":[{"verdict":{"x":1}',  # not a spelled-out ruling
        ):
            assert _repair_truncated_json(cut) is None, cut

    def test_a_whole_nested_decisive_value_is_kept_when_the_cut_follows_it(self):
        closed = '{"verdicts":[{"clause":"A","verdict":"none"},{"clause":"B","verdict":"yes"}'
        assert _repair_truncated_json(closed) == {
            "verdicts": [
                {"clause": "A", "verdict": "none"},
                {"clause": "B", "verdict": "yes"},
            ]
        }
        assert _repair_truncated_json('{"verdicts":[{"pass": true') == {
            "verdicts": [{"pass": True}]
        }

    def test_a_nested_cut_inside_an_explanation_key_is_repaired_as_before(self):
        cut = '{"verdicts":[{"clause":"A","verdict":"none","reason":"the step omit'
        assert _repair_truncated_json(cut) == {
            "verdicts": [{"clause": "A", "verdict": "none", "reason": "the step omit"}]
        }
        in_a_list = '{"pass": false, "overridden_signals": ["SP-001", "SP-0'
        assert _repair_truncated_json(in_a_list) == {
            "pass": False,
            "overridden_signals": ["SP-001", "SP-0"],
        }

    def test_a_ruling_named_twice_at_any_depth_has_no_ruling(self):
        """The duplicate check covers every object, deliberately: the payload fails closed."""
        for payload in (
            '{"verdicts": [{"verdict": "none", "verdict": "yes"}]}',
            '{"verdicts": [{"verdict": "none", "reason": "x", "verdict": "none"}]}',
            '{"verdicts": [{"pass": true, "pass": true}], "pass": true}',
        ):
            assert _extract_json_object(payload) is None, payload

    def test_decisive_property_is_listed_first_in_both_tool_schemas(self):
        """Property order is a hint to the model, not a guarantee (see build_check_tool)."""
        from argus.judge_specs import build_check_tool

        graded = build_check_tool(_spec())["function"]["parameters"]
        assert list(graded["properties"]) == ["verdict", "reason"]
        assert graded["required"] == ["verdict", "reason"]
        pass_fail = build_check_tool(_spec(choices={}))["function"]["parameters"]
        assert list(pass_fail["properties"]) == ["pass", "reason"]
        assert pass_fail["required"] == ["pass", "reason"]
        judge = JUDGE_VERDICT_TOOL["function"]["parameters"]
        assert list(judge["properties"])[0] == "pass"
        assert judge["required"][0] == "pass"

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
class TestEveryParseSiteAppliesTheSameGuards:
    """Whole, fenced, prose-wrapped and cut payloads are read by one set of rules."""

    _RULING_TWICE = [
        '{"pass": true, "pass": false}',
        '{"pass": true, "reason": "x", "pass": false}',
        '{"verdict": "a", "reason": "x", "verdict": "b"}',
    ]

    @pytest.mark.parametrize("payload", _RULING_TWICE)
    def test_a_complete_payload_naming_the_ruling_twice_has_no_ruling(self, payload):
        """``json.loads`` alone keeps the last value, so the reader would pick a side."""
        assert _verdict_from_tool_calls(_tool_call_result(payload)) is None
        assert _extract_json_object(payload) is None
        assert _extract_json_object(f"```json\n{payload}\n```") is None
        assert _extract_json_object(f"My ruling: {payload} Thanks.") is None
        assert _parse_verdict_payload(_tool_call_result(payload)) is None

    def test_a_ruling_named_twice_is_a_skip_for_the_judge(self, monkeypatch):
        _mock_llm(monkeypatch, _tool_call_result('{"pass": true, "pass": false}'))
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.evaluated is False

    def test_a_ruling_named_twice_is_a_skip_for_a_custom_check(self, monkeypatch):
        from argus.judge_specs import run_check_spec

        repeated = '{"verdict": "clean", "reason": "x", "verdict": "leak"}'
        _mock_llm(monkeypatch, _tool_call_result(repeated))
        verdict = run_check_spec(_spec(), "n", {"q": "x"}, {"a": "y"}, model="m")
        assert verdict.evaluated is False
        assert verdict.passed is True  # skips stay non-blocking

    def test_a_single_ruling_beside_nested_objects_still_parses(self):
        payload = json.dumps(
            {
                "pass": False,
                "reason": "x",
                "disambiguation_verdicts": [
                    {"sig_id": "a", "is_failure": True, "confidence": 0.5, "reason": "r"}
                ],
            }
        )
        assert _verdict_from_tool_calls(_tool_call_result(payload))["pass"] is False
        assert _extract_json_object(payload)["pass"] is False

    def test_a_raw_newline_inside_a_string_does_not_cost_the_verdict(self):
        """Models do emit a literal CR/LF inside ``reason``; strict JSON refuses it."""
        for raw in ("a\nb", "a\r\nb", "a\rb"):
            payload = '{"pass": true, "reason": "' + raw + '"}'
            expected = {"pass": True, "reason": raw}
            assert _extract_json_object(payload) == expected
            assert _extract_json_object(f"```json\n{payload}\n```") == expected
            assert _extract_json_object(f"Ruling: {payload} done") == expected
            assert _verdict_from_tool_calls(_tool_call_result(payload)) == expected

    def test_a_raw_newline_in_a_cut_payload_is_still_repaired(self):
        cut = '{"pass": false, "reason": "a\r\nb'
        expected = {"pass": False, "reason": "a\r\nb"}
        assert _verdict_from_tool_calls(_tool_call_result(cut)) == expected
        assert _extract_json_object(cut) == expected

    def test_a_raw_newline_in_the_reason_does_not_skip_the_judge(self, monkeypatch):
        payload = '{"pass": false, "reason": "line one\nline two", "confidence": 0.9}'
        _mock_llm(monkeypatch, _tool_call_result(payload))
        result, _ = check_semantic_coherence("node_a", {"q": "hello"}, {"a": "world"})
        assert result.evaluated is True
        assert result.passed is False
        assert result.reason == "line one\nline two"

    def test_deep_nesting_is_no_verdict_not_a_crash(self):
        """RecursionError, as the docstrings promise: None, never an exception."""
        deep = '{"pass": true, "x": ' + "[" * 100_000 + "]" * 100_000 + "}"
        cut = deep[:-5]
        for payload in (deep, cut):
            assert _extract_json_object(payload) is None
            assert _extract_json_object(f"```json\n{payload}\n```") is None
            assert _verdict_from_tool_calls(_tool_call_result(payload)) is None
            assert _repair_truncated_json(payload) is None


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
        """No content to fall back to: the repaired arguments are the verdict.

        In this payload ``pass`` is spelled out and only the reason was cut.
        """
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

    @staticmethod
    def _google_body(monkeypatch, **kwargs):
        import argus.providers as providers

        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _FakeHTTPResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                }
            )

        monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
        providers.call_google(
            api_key="g-key",
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            **kwargs,
        )
        return captured["body"]

    def test_google_json_mime_is_dropped_when_tools_are_present(self, monkeypatch):
        """Gemini 400s on function calling combined with a JSON response mime type."""
        body = self._google_body(
            monkeypatch,
            response_format={"type": "json_object"},
            tools=[JUDGE_VERDICT_TOOL],
            tool_choice=JUDGE_TOOL_CHOICE,
        )
        assert "responseMimeType" not in body["generationConfig"]
        assert body["tools"][0]["functionDeclarations"][0]["name"] == "record_verdict"
        assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"

    def test_google_json_mime_still_maps_without_tools(self, monkeypatch):
        body = self._google_body(monkeypatch, response_format={"type": "json_object"})
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "tools" not in body

    def test_google_declares_property_ordering_on_every_object_schema(self, monkeypatch):
        """Gemini does not honor JSON-schema property order; ``propertyOrdering`` is its field."""
        from argus.judge_specs import build_check_tool

        graded, pass_fail = build_check_tool(_spec()), build_check_tool(_spec(choices={}))
        tools = [JUDGE_VERDICT_TOOL, graded, pass_fail]
        body = self._google_body(monkeypatch, tools=tools, tool_choice=JUDGE_TOOL_CHOICE)
        judge, graded_params, pass_fail_params = [
            d["parameters"] for d in body["tools"][0]["functionDeclarations"]
        ]
        judge_props = JUDGE_VERDICT_TOOL["function"]["parameters"]["properties"]
        assert judge["propertyOrdering"] == list(judge_props)
        assert judge["propertyOrdering"][0] == "pass"
        nested = judge["properties"]["disambiguation_verdicts"]["items"]
        assert nested["propertyOrdering"] == ["sig_id", "is_failure", "confidence", "reason"]
        assert graded_params["propertyOrdering"] == ["verdict", "reason"]
        assert pass_fail_params["propertyOrdering"] == ["pass", "reason"]
        # only object schemas carry it, never a leaf
        assert "propertyOrdering" not in judge["properties"]["pass"]

    def test_google_schema_conversion_keeps_a_property_called_properties(self):
        """``properties`` is a name -> schema map: a property may share a keyword's name."""
        from argus.providers import _google_schema_type

        schema = {
            "type": "object",
            "properties": {"properties": {"type": "string"}, "type": {"type": "boolean"}},
        }
        out = _google_schema_type(schema)
        assert out["properties"] == {"properties": {"type": "STRING"}, "type": {"type": "BOOLEAN"}}
        assert out["propertyOrdering"] == ["properties", "type"]

    def test_other_providers_never_see_property_ordering(self, monkeypatch):
        import argus.providers as providers

        sent: dict[str, dict] = {}

        def fake_urlopen(req, timeout=None):
            sent[req.full_url] = json.loads(req.data)
            return _FakeHTTPResponse(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

        monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
        kwargs = {
            "api_key": "k",
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [JUDGE_VERDICT_TOOL],
            "tool_choice": JUDGE_TOOL_CHOICE,
        }
        providers.call_openai(**kwargs)
        providers.call_anthropic(**kwargs)
        providers.call_google(**kwargs)

        by_host = {url.split("/")[2]: json.dumps(body) for url, body in sent.items()}
        assert len(by_host) == 3
        assert "propertyOrdering" in by_host["generativelanguage.googleapis.com"]
        for host, text in by_host.items():
            if host != "generativelanguage.googleapis.com":
                assert "propertyOrdering" not in text, host
        # the shared tool definition is converted by copy, never edited in place
        assert "propertyOrdering" not in json.dumps(JUDGE_VERDICT_TOOL)


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

    def test_unhashable_severity_or_when_skips_only_that_file(self, tmp_path, caplog):
        """``[] in frozenset`` raises TypeError; one bad file must not hide the rest."""
        import logging

        from argus.judge_specs import load_check_specs

        d = self._write(tmp_path, "a_sev.json", {"name": "a", "prompt": "p", "severity": []})
        self._write(tmp_path, "b_when.json", {"name": "b", "prompt": "p", "when": {}})
        self._write(tmp_path, "c_good.json", {"name": "ok", "prompt": "p"})
        with caplog.at_level(logging.WARNING, logger="argus"):
            specs = load_check_specs(d)
        assert [s.name for s in specs] == ["ok"]
        assert "a_sev.json" in caplog.text and "b_when.json" in caplog.text

    def test_deeply_nested_json_skips_only_that_file(self, tmp_path, caplog):
        """json.loads raises RecursionError (not a JSONDecodeError) on this input."""
        import logging

        from argus.judge_specs import load_check_specs

        d = self._write(tmp_path, "a_deep.json", "[" * 100_000)
        self._write(tmp_path, "b_good.json", {"name": "ok", "prompt": "p"})
        with caplog.at_level(logging.WARNING, logger="argus"):
            specs = load_check_specs(d)
        assert [s.name for s in specs] == ["ok"]
        assert "a_deep.json" in caplog.text

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

    def test_truncated_enum_value_is_not_a_verdict(self, monkeypatch):
        """``clean`` is a real choice, but here it is ``clean_with_warnings`` cut short."""
        from argus.judge_specs import run_check_spec

        spec = _spec(
            choices={
                "clean": {"score": 1.0, "label": "clean"},
                "clean_with_warnings": {"score": 0.2, "label": "warnings"},
            }
        )
        _mock_llm(monkeypatch, _tool_call_result('{"reason": "minor omission", "verdict": "clean'))
        verdict = run_check_spec(spec, "n", {"q": "x"}, {"a": "y"}, model="m")
        assert verdict.evaluated is False
        assert verdict.passed is True  # skips stay non-blocking

        _mock_llm(monkeypatch, _tool_call_result('{"verdict": "clean_with_war'))
        assert run_check_spec(spec, "n", {"q": "x"}, {"a": "y"}, model="m").evaluated is False

    def test_a_repeated_verdict_key_is_not_a_verdict(self, monkeypatch):
        """A cut ``clean`` after ``clean_with_warnings`` would otherwise score as a pass."""
        from argus.judge_specs import run_check_spec

        spec = _spec(
            choices={
                "clean": {"score": 1.0, "label": "clean"},
                "clean_with_warnings": {"score": 0.2, "label": "warnings"},
            }
        )
        repeated = '{"verdict": "clean_with_warnings", "reason": "x", "verdict": "clean'
        _mock_llm(monkeypatch, _tool_call_result(repeated))
        verdict = run_check_spec(spec, "n", {"q": "x"}, {"a": "y"}, model="m")
        assert verdict.evaluated is False
        assert verdict.passed is True  # skips stay non-blocking

    def test_a_closed_verdict_with_a_cut_reason_is_kept(self, monkeypatch):
        from argus.judge_specs import run_check_spec

        spec = _spec(
            choices={
                "clean": {"score": 1.0, "label": "clean"},
                "clean_with_warnings": {"score": 0.2, "label": "warnings"},
            }
        )
        _mock_llm(
            monkeypatch,
            _tool_call_result('{"verdict": "clean_with_warnings", "reason": "minor omiss'),
        )
        verdict = run_check_spec(spec, "n", {"q": "x"}, {"a": "y"}, model="m")
        assert verdict.evaluated is True
        assert verdict.verdict == "clean_with_warnings"
        assert verdict.score == 0.2
        assert verdict.reason == "minor omiss"

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
    """Spec verdict -> SemanticSignal via ArgusSession._run_custom_check."""

    def _session(self):
        from argus.session import ArgusSession

        return ArgusSession()

    def test_verdict_becomes_semantic_signal(self, monkeypatch, tmp_path):
        _mock_llm(monkeypatch, _check_tool_result({"verdict": "leak", "reason": "SSN found"}))

        sig = self._session()._run_custom_check(_spec(), "node_a", {"q": "x"}, {"a": "ssn"})

        assert sig.sig_id == "check:no_pii"
        assert sig.category == "custom_check"
        assert sig.severity == "warning"  # the spec's severity
        assert sig.evidence == "leak"
        assert sig.confidence == 0.0  # the choice's score
        assert sig.description == "SSN found"

    def test_passing_check_attaches_nothing(self, monkeypatch, tmp_path):
        """A pass is not a finding: no info signal, no Finding per clean step."""
        _mock_llm(monkeypatch, _check_tool_result({"verdict": "clean", "reason": "fine"}))

        sig = self._session()._run_custom_check(_spec(), "node_a", {"q": "x"}, {"a": "clean"})
        assert sig is None

    def test_unevaluated_verdict_attaches_nothing(self, monkeypatch, tmp_path):
        _mock_llm(monkeypatch, {"choices": [{"message": {"content": "garbage"}}], "usage": {}})

        sig = self._session()._run_custom_check(_spec(), "node_a", {"q": "x"}, {"a": "y"})
        assert sig is None

    def test_get_check_specs_logs_a_loader_failure(self, monkeypatch, caplog):
        """A spec directory that cannot be loaded disables custom checks, loudly."""
        import logging

        def boom(_directory):
            raise RuntimeError("checks dir exploded")

        monkeypatch.setattr("argus.judge_specs.load_check_specs", boom)
        session = self._session()
        with caplog.at_level(logging.WARNING, logger="argus"):
            assert session._get_check_specs() == []
        assert "checks dir exploded" in caplog.text
        assert session._check_specs == [], "cached: the failure is not retried per node"


_CLEAN_OUT = {"summary": "Q3 revenue grew 12%."}
# Long enough that no shape rule (shallow_output) fires.
_LONG_OUT = {
    "summary": "Q3 revenue grew 12% on enterprise demand and margins improved in every region."
}
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
    behavior_type=None,
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
        behavior_type=behavior_type,
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
        calls = _install_llm(monkeypatch, check=_LEAK)
        _, step = _run_step(tmp_path, output=_CLEAN_OUT, on_judge_failure=on_judge_failure)

        assert len(calls["check"]) == 1, "once per step per spec, never twice"
        assert calls["coherence"] == [], "a clean step is not the coherence judge's business"
        assert _sig_ids(step) == ["check:no_pii"]
        assert step.status == "pass"

    @_BOTH_JUDGE_PATHS
    def test_passing_check_attaches_nothing(self, monkeypatch, tmp_path, on_judge_failure):
        """The check ran, answered clean: no signal, no Finding (no noise per clean step)."""
        calls = _install_llm(monkeypatch, check={"verdict": "clean", "reason": "no PII"})
        record, step = _run_step(tmp_path, output=_CLEAN_OUT, on_judge_failure=on_judge_failure)

        assert len(calls["check"]) == 1
        assert _sig_ids(step) == []
        assert [f for f in record.findings if f.type == "custom_check"] == []

    @_BOTH_JUDGE_PATHS
    def test_custom_checks_do_not_run_with_semantic_check_off(
        self, monkeypatch, tmp_path, on_judge_failure
    ):
        """The judge switch is the consent for sending node I/O to the provider."""
        calls = _install_llm(monkeypatch, check=_LEAK)
        _, step = _run_step(
            tmp_path, output=_TODO_OUT, semantic_check=False, on_judge_failure=on_judge_failure
        )

        assert calls == {"check": [], "coherence": []}, "no LLM call of any kind"
        assert "check:no_pii" not in _sig_ids(step)

    def test_custom_check_needs_llm_investigation_enabled(self, monkeypatch, tmp_path):
        calls = _install_llm(monkeypatch, check=_LEAK)
        _, step = _run_step(tmp_path, output=_CLEAN_OUT, enabled=False)

        assert calls["check"] == []
        assert step.inspection.semantic_signals == []

    def test_warning_signal_spec_follows_its_own_gate(self, monkeypatch, tmp_path):
        spec = {**_PII_SPEC, "when": "warning_signal"}
        calls = _install_llm(monkeypatch, check=_LEAK)

        _run_step(tmp_path, output=_CLEAN_OUT, specs=[spec])
        assert calls["check"] == [], "no heuristic signal on the step"

        _run_step(tmp_path, output=_TODO_OUT, specs=[spec])
        assert len(calls["check"]) == 1

    def test_warning_signal_spec_is_skipped_per_spec(self, monkeypatch, tmp_path):
        """A gated-out spec does not stop the specs beside it."""
        calls = _install_llm(monkeypatch, check=_LEAK)
        gated = {**_PII_SPEC, "name": "only_with_flags", "when": "warning_signal"}
        always = {**_PII_SPEC, "name": "always"}

        _, step = _run_step(
            tmp_path,
            output=_CLEAN_OUT,
            specs=[
                {**gated, "prompt": "GATED {{output}}"},
                {**always, "prompt": "ALWAYS {{output}}"},
            ],
        )
        asked = [call["messages"][-1]["content"].split()[0] for call in calls["check"]]
        assert asked == ["ALWAYS"]
        assert _sig_ids(step) == ["check:always"]

    def test_two_failing_specs_on_one_node_yield_two_findings(self, monkeypatch, tmp_path):
        """Both signals share category and node; their Finding ids must still differ."""
        _install_llm(monkeypatch, check=_LEAK)
        specs = [{**_PII_SPEC, "name": "no_pii"}, {**_PII_SPEC, "name": "no_secrets"}]

        record, step = _run_step(tmp_path, output=_CLEAN_OUT, specs=specs)
        again, _ = _run_step(tmp_path, output=_CLEAN_OUT, specs=specs)

        assert sorted(_sig_ids(step)) == ["check:no_pii", "check:no_secrets"]
        found = [f for f in record.findings if f.type == "custom_check"]
        assert len(found) == 2
        assert len({f.id for f in found}) == 2
        for name in ("no_pii", "no_secrets"):
            assert sum(f"check:{name}" in f.reason for f in found) == 1
        # content hashes: the same graph and specs give the same ids next run
        assert sorted(f.id for f in found) == sorted(
            f.id for f in again.findings if f.type == "custom_check"
        )

    def test_a_slow_or_broken_spec_only_loses_its_own_signal(self, monkeypatch, tmp_path, caplog):
        """One future per (node, spec): a hung or raising spec cannot take the rest down."""
        import logging
        import threading

        import argus.session as session_mod

        _install_llm(monkeypatch, check=_LEAK)
        # `slow` is parked on this event until teardown, so it can only ever hit
        # the drain deadline. That deadline (3 s, patched below) is the one clock
        # this test depends on: `boom` / `fast` return at once, orders of
        # magnitude inside it, but a runner stalled for 3 s could still lose them.
        release = threading.Event()

        def fake_run_check_spec(spec, node_name, input_state, output_dict, model, api_key=None):
            if spec.name == "slow":
                release.wait(30)
            if spec.name == "boom":
                raise RuntimeError("spec exploded")
            return CheckVerdict(
                check_name=spec.name,
                verdict="leak",
                score=0.0,
                passed=False,
                reason="found",
                severity="warning",
                prompt_tokens=0,
                completion_tokens=0,
            )

        monkeypatch.setattr("argus.judge_specs.run_check_spec", fake_run_check_spec)
        monkeypatch.setattr(session_mod, "_CUSTOM_CHECK_TIMEOUT_S", 3.0)
        specs = [{**_PII_SPEC, "name": n} for n in ("boom", "fast", "slow")]
        try:
            with caplog.at_level(logging.WARNING, logger="argus"):
                _, step = _run_step(tmp_path, output=_CLEAN_OUT, specs=specs)
        finally:
            release.set()

        assert _sig_ids(step) == ["check:fast"]
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        # a raising spec is named with its error; a spec still running at the
        # deadline is named in the one "dropped" warning
        assert any(
            "custom check boom on summarize" in m and "spec exploded" in m for m in messages
        )
        assert any("dropped 1" in m and "slow on summarize" in m for m in messages)

    @pytest.mark.parametrize("severity", ["warning", "critical"])
    def test_failing_custom_check_cannot_move_the_correlators_origin(
        self, monkeypatch, tmp_path, severity
    ):
        """A custom signal is not structural evidence: the behavioral-only cap stays."""
        _install_llm(monkeypatch, check=_LEAK)
        # BA-005 only: a text answer from a node declared as a retrieval result.
        kwargs = {"output": _LONG_OUT, "behavior_type": "retrieval_result"}

        plain, _ = _run_step(tmp_path, specs=[], **kwargs)
        checked, step = _run_step(tmp_path, specs=[{**_PII_SPEC, "severity": severity}], **kwargs)

        assert "check:no_pii" in _sig_ids(step)
        (base,) = plain.correlation.degradation_origins
        (origin,) = checked.correlation.degradation_origins
        assert base.confidence == 0.4, "behavioral-only origins are capped"
        assert origin.confidence == base.confidence
        assert origin.reason == base.reason
        assert origin.signal_types == base.signal_types
        assert checked.first_failure_step == plain.first_failure_step

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

    def test_recorder_default_path_runs_custom_checks(self, monkeypatch, tmp_path):
        """ArgusRecorder with a key configured turns the judge switch on, so the gate opens."""
        pytest.importorskip("langgraph")
        from typing import TypedDict

        from langgraph.graph import END, START, StateGraph

        from argus.recorder import ArgusRecorder
        from argus.storage import load_run

        class _State(TypedDict, total=False):
            query: str
            summary: str

        def build():
            g = StateGraph(_State)
            g.add_node("summarize", lambda s: dict(_CLEAN_OUT))
            g.add_edge(START, "summarize")
            g.add_edge("summarize", END)
            return g.compile()

        calls = _install_llm(monkeypatch, check=_LEAK)
        checks = tmp_path / ".argus" / "checks"
        checks.mkdir(parents=True)
        (checks / "no_pii.json").write_text(json.dumps(_PII_SPEC), encoding="utf-8")

        recorder = ArgusRecorder()  # semantic_judge unset: the key decides
        monitored = recorder.attach(build())
        cfg = recorder._new_session()._llm_investigation_config
        assert cfg.enabled and cfg.semantic_check
        monitored.invoke({"query": "q"})
        assert len(calls["check"]) == 1
        record = load_run(recorder.session.run_id)
        assert [f.type for f in record.findings if f.type == "custom_check"] == ["custom_check"]

        calls["check"].clear()
        off = ArgusRecorder(semantic_judge=False)
        off.attach(build()).invoke({"query": "q"})
        assert calls == {"check": [], "coherence": []}, "judge switched off: nothing is sent"


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
        # The hosted proxy is opt-in: prompts carry real ledger values.
        monkeypatch.setenv("ARGUS_HOSTED_LLM", "1")
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


@pytest.mark.integration
class TestCustomChecksStayOffTheJudgePool:
    """A slow check must never delay or drop a coherence verdict."""

    def test_custom_checks_never_queue_ahead_of_coherence_verdicts(self, monkeypatch, tmp_path):
        """10 specs x 3 nodes, every check parked until every verdict is in.

        On one shared 4-worker pool the later nodes' verdicts queue behind the
        earlier nodes' checks, and those checks are waiting for exactly those
        verdicts, so they run out their wait first. Event-driven: the result
        does not depend on how fast the runner is.
        """
        import threading

        from argus.models import ArgusConfig, LLMInvestigationConfig
        from argus.session import ArgusSession
        from argus.storage import load_run

        nodes = ["n0", "n1", "n2"]
        checks = tmp_path / ".argus" / "checks"
        checks.mkdir(parents=True)
        for i in range(10):
            spec = {**_PII_SPEC, "name": f"s{i}"}
            (checks / f"s{i}.json").write_text(json.dumps(spec), encoding="utf-8")

        all_judged = threading.Event()
        judged: list[dict] = []

        def fake_llm(**kwargs):
            judged.append(kwargs)  # list.append is atomic
            if len(judged) == len(nodes):
                all_judged.set()
            return _tool_call_result({"pass": True, "reason": "fine", "confidence": 0.9})

        waits: list[bool] = []

        def parked_check(spec, node_name, input_state, output_dict, model, api_key=None):
            waits.append(all_judged.wait(30))
            return CheckVerdict(
                check_name=spec.name,
                verdict="leak",
                score=0.0,
                passed=False,
                reason="found",
                severity="warning",
                prompt_tokens=0,
                completion_tokens=0,
            )

        monkeypatch.setattr("argus.llm_proxy.is_available", lambda: True)
        monkeypatch.setattr("argus.llm_proxy.create_chat_completion", fake_llm)
        monkeypatch.setattr("argus.judge_specs.run_check_spec", parked_check)

        session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True),
            config=ArgusConfig(on_judge_failure="warn"),
        )
        wrapped = {n: session.wrap(n, lambda state: dict(_TODO_OUT)) for n in nodes}
        session.set_node_names(nodes)
        session.set_edges({})
        for n in nodes:
            wrapped[n]({"query": "q"})
        session.finalize()
        record = load_run(session.run_id)

        assert len(waits) == 30
        assert all(waits), "a check ran out its wait: the verdicts were queued behind it"
        assert len(judged) == len(nodes)
        for step in record.steps:
            assert step.semantic_check is not None and step.semantic_check.evaluated
            assert len(_sig_ids(step)) == 10, "and every check still attached its signal"

    def test_a_lost_coherence_verdict_is_logged_not_silent(self, monkeypatch, tmp_path, caplog):
        import logging

        from argus.session import ArgusSession

        def boom(self, *args, **kwargs):
            raise RuntimeError("judge exploded")

        _install_llm(monkeypatch, check=_LEAK)
        monkeypatch.setattr(ArgusSession, "_run_judge_sync", boom)
        with caplog.at_level(logging.WARNING, logger="argus"):
            _, step = _run_step(tmp_path, output=_TODO_OUT, specs=[])

        assert step.semantic_check is None, "the lost verdict is still dropped, not invented"
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("summarize" in m and "judge exploded" in m for m in messages)

    def test_the_check_pool_is_released_with_the_judge_pool(self, monkeypatch, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        from argus.models import LLMInvestigationConfig
        from argus.session import ArgusSession

        _install_llm(monkeypatch, check=_LEAK)
        checks = tmp_path / ".argus" / "checks"
        checks.mkdir(parents=True)
        (checks / "no_pii.json").write_text(json.dumps(_PII_SPEC), encoding="utf-8")
        session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True)
        )
        shutdowns: dict[int, tuple] = {}
        real_shutdown = ThreadPoolExecutor.shutdown

        def spy(self, wait=True, **kwargs):
            shutdowns[id(self)] = (wait, kwargs)
            return real_shutdown(self, wait=wait, **kwargs)

        monkeypatch.setattr(ThreadPoolExecutor, "shutdown", spy)
        session.wrap("summarize", lambda state: dict(_TODO_OUT))({"query": "q"})
        judge_pool, check_pool = session._judge_pool, session._check_pool
        try:
            assert judge_pool is not None and check_pool is not None
            assert judge_pool is not check_pool
        finally:  # a failed assert above must not leave the pools' threads behind
            session.finalize()
        assert session._judge_pool is None and session._check_pool is None
        # Neither pool is cancelled wholesale: a second finalize on the same pool
        # would cancel what another drain is still waiting for. A drain cancels
        # only the futures it gave up on (see the tests below).
        for pool in (judge_pool, check_pool):
            assert shutdowns[id(pool)] == (False, {})


class _ParkedChecks:
    """A session whose custom checks block on ``release``, except on ``fast_nodes``.

    No check touches a clock: a parked one ends only when the test sets
    ``release`` (its own 30 s wait is a safety net for a failing test, not a
    timing any assertion leans on). The one clock is the drain's ``budget``:
    a test that needs a fast check to finish inside it gives it seconds, and a
    parked check then simply outlasts it. ``pool_full`` is set once four
    checks, the pool's whole width, are inside ``run_check_spec``.
    """

    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        nodes,
        spec_names,
        budget,
        fast_nodes=(),
        auto_finalize=True,
    ):
        import threading

        import argus.session as session_mod
        from argus.models import ArgusConfig, LLMInvestigationConfig
        from argus.session import ArgusSession

        checks = tmp_path / ".argus" / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        for name in spec_names:
            spec = {**_PII_SPEC, "name": name}
            (checks / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")
        self.nodes = list(nodes)
        self.release = threading.Event()
        self.pool_full = threading.Event()
        self.started: list[str] = []  # list.append is atomic

        def fake_check(spec, node_name, input_state, output_dict, model, api_key=None):
            self.started.append(f"{spec.name} on {node_name}")
            if len(self.started) >= 4:
                self.pool_full.set()
            if node_name not in fast_nodes:
                self.release.wait(30)
            return CheckVerdict(
                check_name=spec.name,
                verdict="leak",
                score=0.0,
                passed=False,
                reason="found",
                severity="warning",
                prompt_tokens=0,
                completion_tokens=0,
            )

        monkeypatch.setattr("argus.judge_specs.run_check_spec", fake_check)
        monkeypatch.setattr(session_mod, "_CUSTOM_CHECK_TIMEOUT_S", budget)
        self.session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True),
            config=ArgusConfig(on_judge_failure="warn"),
        )
        self.wrapped = {n: self.session.wrap(n, lambda state: dict(_CLEAN_OUT)) for n in nodes}
        if auto_finalize:  # the last node's return then runs finalize
            self.session.set_node_names(self.nodes)
            self.session.set_edges({})

    def close(self):
        """Release every parked check and wait for the workers to leave."""
        import threading

        self.release.set()
        for thread in threading.enumerate():
            if thread.name.startswith("argus-check"):
                thread.join(30)


@pytest.mark.integration
class TestCustomCheckDrainIsBounded:
    """Finalize runs inside the last node's return: it may not wait out every check."""

    def test_finalize_gives_up_at_one_deadline_not_one_per_check(
        self, monkeypatch, tmp_path, caplog
    ):
        """12 nodes x 3 specs, every check after the first node parked.

        A wait per check (10 s each in production) made the last node's return
        block for minutes. 33 checks stay parked and the budget is 1 s, so a
        per-check wait costs 30 s or more and one shared deadline costs 1 s; the
        bound below is generous on both sides.
        """
        import logging
        import time

        from argus.storage import load_run

        parked = _ParkedChecks(
            monkeypatch,
            tmp_path,
            nodes=[f"n{i}" for i in range(12)],
            spec_names=["a", "b", "c"],
            fast_nodes=("n0",),
            budget=1.0,
        )
        try:
            for node in parked.nodes[:-1]:
                parked.wrapped[node]({"query": "q"})
            started = time.monotonic()
            with caplog.at_level(logging.WARNING, logger="argus"):
                parked.wrapped[parked.nodes[-1]]({"query": "q"})  # auto-finalize
            elapsed = time.monotonic() - started
            assert not parked.release.is_set(), "persisted while the checks were still parked"
            record = load_run(parked.session.run_id)
        finally:
            parked.close()

        assert elapsed < 10, f"the last node's return waited {elapsed:.1f}s on parked checks"
        assert sorted(_sig_ids(record.steps[0])) == ["check:a", "check:b", "check:c"]
        assert all(_sig_ids(step) == [] for step in record.steps[1:])
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        dropped = [m for m in messages if "custom check" in m]
        assert len(dropped) == 1, dropped
        assert "33" in dropped[0] and "dropped" in dropped[0]

    def test_a_late_result_is_dropped_never_attached(self, monkeypatch, tmp_path):
        """n1's check is parked past the drain's deadline; n0's had already finished.

        The budget is short because n1's check outlasts it whatever it is, and
        it cannot make n0's attach flaky: n0's check is done before the drain
        starts, so the drain finds it finished at any budget.
        """
        from argus.storage import load_run

        parked = _ParkedChecks(
            monkeypatch,
            tmp_path,
            nodes=["n0", "n1"],
            spec_names=["a"],
            fast_nodes=("n0",),
            budget=0.3,
        )
        try:
            parked.wrapped["n0"]({"query": "q"})
            (_, _, n0_check), = parked.session._pending_custom_checks
            n0_check.result(30)  # n0's check is finished before n1's return drains
            parked.wrapped["n1"]({"query": "q"})
            record = load_run(parked.session.run_id)
        finally:
            parked.close()  # n1's check now finishes, after the run was persisted

        assert _sig_ids(record.steps[1]) == []
        in_memory = [_sig_ids(event) for event in parked.session._events]
        assert in_memory == [["check:a"], []], "the late 'leak' was not attached afterwards"

    def test_checks_abandoned_at_the_deadline_never_start_later(self, monkeypatch, tmp_path):
        """4 workers, 10 parked checks: the 6 still queued at finalize must stay unrun."""
        parked = _ParkedChecks(
            monkeypatch,
            tmp_path,
            nodes=[f"n{i}" for i in range(5)],
            spec_names=["a", "b"],
            budget=0.2,
            auto_finalize=False,
        )
        try:
            for node in parked.nodes:
                parked.wrapped[node]({"query": "q"})
            assert parked.pool_full.wait(30), "the four workers never all started a check"
            futures = [future for _, _, future in parked.session._pending_custom_checks]
            assert len(futures) == 10
            parked.session.finalize()
            cancelled = [future for future in futures if future.cancelled()]
        finally:
            parked.close()

        assert len(cancelled) == 6
        assert len(parked.started) == 4, "a queued check ran after finalize returned"


@pytest.mark.integration
class TestCheckPoolSurvivesFinalizeRacingASubmit:
    """Finalize may run on another thread: it must never raise into a user's node."""

    def _session(self, monkeypatch, tmp_path):
        from argus.models import LLMInvestigationConfig
        from argus.session import ArgusSession

        _install_llm(monkeypatch, check=_LEAK)
        checks = tmp_path / ".argus" / "checks"
        checks.mkdir(parents=True)
        (checks / "no_pii.json").write_text(json.dumps(_PII_SPEC), encoding="utf-8")
        return ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True)
        )

    def test_a_pool_that_is_already_shut_down_skips_the_check(self, monkeypatch, tmp_path, caplog):
        import logging
        from concurrent.futures import ThreadPoolExecutor

        session = self._session(monkeypatch, tmp_path)
        dead = ThreadPoolExecutor(max_workers=1)
        dead.shutdown()
        session._check_pool = dead

        with caplog.at_level(logging.WARNING, logger="argus"):
            out = session.wrap("summarize", lambda state: dict(_CLEAN_OUT))({"query": "q"})

        assert out == _CLEAN_OUT, "the user's node returned its output"
        assert [e.status for e in session._events] == ["pass"], "and was not recorded as a crash"
        assert session._pending_custom_checks == []
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("no_pii on summarize" in m and "skipped" in m for m in messages)

    def test_finalize_on_another_thread_between_the_pool_lookup_and_the_submit(
        self, monkeypatch, tmp_path, caplog
    ):
        import logging
        import threading

        import argus.session as session_mod

        session = self._session(monkeypatch, tmp_path)
        entered, proceed = threading.Event(), threading.Event()

        class GatedPool(session_mod.ThreadPoolExecutor):
            """The check pool holds every submit until the test lets it through."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._gated = kwargs.get("thread_name_prefix") == "argus-check"

            def submit(self, *args, **kwargs):
                if self._gated:
                    entered.set()
                    proceed.wait(30)
                return super().submit(*args, **kwargs)

        monkeypatch.setattr(session_mod, "ThreadPoolExecutor", GatedPool)
        errors: list[BaseException] = []

        def node():
            try:
                session.wrap("summarize", lambda state: dict(_CLEAN_OUT))({"query": "q"})
            except BaseException as exc:  # what the user's node would have seen
                errors.append(exc)

        thread = threading.Thread(target=node)
        thread.start()
        try:
            with caplog.at_level(logging.WARNING, logger="argus"):
                assert entered.wait(30), "the node never reached the submit"
                session._apply_deferred_judges()  # another thread's finalize, mid-submit
                assert session._check_pool is None
                proceed.set()
                thread.join(30)
        finally:
            proceed.set()
            thread.join(30)

        assert not thread.is_alive()
        assert errors == []
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("no_pii on summarize" in m and "skipped" in m for m in messages)

    def test_a_node_ending_after_a_crash_finalize_starts_no_pool_and_no_check(
        self, monkeypatch, tmp_path, caplog
    ):
        """A sibling branch ends after another branch's crash has finalized the run.

        Nothing drains a future submitted now, and a pool created now is never
        shut down, so interpreter exit would join a check that nobody reads.
        """
        import logging

        session = self._session(monkeypatch, tmp_path)

        def boom(state):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            session.wrap("a", boom)({"query": "q"})
        assert session._completed, "the crash finalized the run"

        with caplog.at_level(logging.WARNING, logger="argus"):
            out = session.wrap("b", lambda state: dict(_TODO_OUT))({"query": "q"})

        assert out == _TODO_OUT, "the user's node is untouched"
        assert session._judge_pool is None and session._check_pool is None
        assert session._pending_judges == [] and session._pending_custom_checks == []
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("no_pii on b" in m and "skipped" in m for m in messages)
        assert any("judge" in m and "'b'" in m and "skipped" in m for m in messages)

    @pytest.mark.parametrize("reopen", ["begin_new_run", "reset_for_resume"])
    def test_a_new_run_on_the_same_session_gets_its_pools_back(
        self, monkeypatch, tmp_path, reopen
    ):
        session = self._session(monkeypatch, tmp_path)
        session.wrap("a", lambda state: dict(_TODO_OUT))({"query": "q"})
        session.finalize()
        assert session._judge_pool is None and session._check_pool is None

        if reopen == "begin_new_run":
            session.begin_new_run()
        else:
            session.reset_for_resume("parent")
        session.wrap("b", lambda state: dict(_TODO_OUT))({"query": "q"})
        try:
            assert session._judge_pool is not None and session._check_pool is not None
            assert len(session._pending_judges) == 1
            assert len(session._pending_custom_checks) == 1
        finally:
            session.finalize()

    def test_a_judge_that_loses_the_pool_race_is_skipped_not_raised_into_the_node(
        self, monkeypatch, tmp_path, caplog
    ):
        """Finalize swaps the judge pool out between the node's lookup and its submit.

        That raised ``cannot schedule new futures after shutdown`` inside
        ``on_node_end``, where the wrapper recorded a second, ``crashed`` event
        for the node and re-raised, so the node's own output was lost.
        """
        import logging

        import argus.session as session_mod

        session = self._session(monkeypatch, tmp_path)

        class RacingPool(session_mod.ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                if self._thread_name_prefix == "argus-judge":
                    session._apply_deferred_judges()  # another thread's finalize
                return super().submit(*args, **kwargs)

        monkeypatch.setattr(session_mod, "ThreadPoolExecutor", RacingPool)
        with caplog.at_level(logging.WARNING, logger="argus"):
            out = session.wrap("summarize", lambda state: dict(_TODO_OUT))({"query": "q"})

        assert out == _TODO_OUT, "the user's node returned its output"
        assert [(e.node_name, e.status) for e in session._events] == [("summarize", "pass")]
        assert session._pending_judges == []
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("judge" in m and "'summarize'" in m and "skipped" in m for m in messages)
        session.finalize()

    def test_a_check_submitted_after_the_drain_took_its_snapshot_is_cancelled_and_logged(
        self, monkeypatch, tmp_path, caplog
    ):
        """A submit that lands between the drain's snapshot and its pool swap.

        Nothing waits for it, so without this it would run on (and the exit
        would join it), unseen. It is counted in one warning and cancelled.
        """
        import logging
        import threading
        from concurrent.futures import Future

        import argus.session as session_mod

        session = self._session(monkeypatch, tmp_path)
        submitting, proceed, appended = (threading.Event() for _ in range(3))
        late: list[Future] = []

        class LatePool(session_mod.ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                future = Future()  # never started: only the drain can settle it
                late.append(future)
                submitting.set()
                proceed.wait(30)
                return future

        class Appended(list):
            def append(self, item):
                super().append(item)
                appended.set()

        real_wait = session_mod._wait_for_futures

        def wait_as_the_node_submits(futures, timeout=None):
            proceed.set()  # the drain has its (empty) snapshot; the node's submit returns now
            assert appended.wait(30), "the late node never queued its check"
            return real_wait(futures, timeout=timeout)

        session._pending_custom_checks = Appended()
        session._check_pool = LatePool(max_workers=1)
        monkeypatch.setattr(session_mod, "_wait_for_futures", wait_as_the_node_submits)
        node = threading.Thread(
            target=lambda: session.wrap("late", lambda state: dict(_CLEAN_OUT))({"query": "q"})
        )
        node.start()
        try:
            assert submitting.wait(30), "the node never reached the submit"
            with caplog.at_level(logging.WARNING, logger="argus"):
                session._apply_deferred_judges()
        finally:
            proceed.set()
            node.join(30)

        assert not node.is_alive()
        assert len(late) == 1 and late[0].cancelled()
        assert session._pending_custom_checks == []
        assert [e.inspection.semantic_signals for e in session._events] == [[]], "not attached"
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        late_warnings = [m for m in messages if "after finalize" in m]
        assert len(late_warnings) == 1 and "1 " in late_warnings[0], messages
        session.finalize()

    @pytest.mark.parametrize("kind", ["judge", "check"])
    def test_a_drain_between_the_submit_and_the_register_leaves_nothing_running(
        self, monkeypatch, tmp_path, caplog, kind
    ):
        """The submit returned, a whole drain ran, and only then did the node queue its entry.

        The submit and the pending-list append were two steps. A drain in the
        gap found nothing to cancel and closed the run; the entry was appended
        after it, so the check (or verdict) ran after finalize returned, was
        never attached, and nothing said so. The append now re-checks the
        closed flag in the same step: the future is cancelled and the skip is
        logged. Event-driven: the node is parked at its first take of the
        pending lock, which is the register, and the drain runs inside that gap.
        """
        import logging
        import threading
        from concurrent.futures import Future, ThreadPoolExecutor

        session = self._session(monkeypatch, tmp_path)
        gate, at_register, drained = (threading.Event() for _ in range(3))
        ran: list[str] = []
        futures: list[Future] = []

        class Recording(ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                futures.append(future)
                return future

        class GapLock:
            """The pending lock, except the node waits before its first take of it."""

            def __init__(self):
                self._lock = threading.Lock()
                self._gapped = False

            def acquire(self, *args, **kwargs):
                if threading.current_thread().name == "node" and not self._gapped:
                    self._gapped = True
                    at_register.set()
                    drained.wait(30)
                return self._lock.acquire(*args, **kwargs)

            def release(self):
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()

        pool = Recording(max_workers=1)
        pool.submit(gate.wait, 30)  # the only worker: whatever is queued behind it waits
        futures.clear()
        session._judge_pool = session._check_pool = pool
        session._run_judge_sync = lambda *args, **kwargs: ran.append("judge") or (None, [])
        session._run_custom_check = lambda *args, **kwargs: ran.append("check")
        session._pending_judges_lock = GapLock()
        output = _TODO_OUT if kind == "judge" else _CLEAN_OUT
        node = threading.Thread(
            target=lambda: session.wrap("late", lambda state: dict(output))({"query": "q"}),
            name="node",
        )
        node.start()
        try:
            assert at_register.wait(30), "the node never got as far as queueing its entry"
            with caplog.at_level(logging.WARNING, logger="argus"):
                session._apply_deferred_judges()  # a whole drain, inside the node's gap
                drained.set()
                node.join(30)
        finally:
            drained.set()
            gate.set()
            node.join(30)
            pool.shutdown(wait=True)  # whatever was not cancelled has run by now

        assert not node.is_alive()
        assert len(futures) == 1 and futures[0].cancelled()
        assert ran == [], "an entry registered after the drain ran after finalize returned"
        assert session._pending_judges == [] and session._pending_custom_checks == []
        what = "judge" if kind == "judge" else "custom check"
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(what in m and "late" in m and "skipped" in m for m in messages), messages

    def test_a_drain_that_outlives_its_run_leaves_the_next_runs_pools_and_pending_alone(
        self, monkeypatch, tmp_path, caplog
    ):
        """Run A's drain is parked on a check; run B begins and queues its own.

        When A's check let go, the drain swapped the pools out, set the closed
        flag and cleared the pending lists: B's check was lost and every later
        submit in B returned "already finalized". The drain carries the
        generation its snapshot belongs to; a run that began since is not its
        to close.
        """
        import logging
        import threading

        import argus.session as session_mod

        session = self._session(monkeypatch, tmp_path)
        gate_a, waiting = threading.Event(), threading.Event()
        real_wait = session_mod._wait_for_futures

        def wait_as_the_drain_parks(futures, timeout=None):
            waiting.set()
            return real_wait(futures, timeout=timeout)

        def check(spec, node_name, input_snap, output_snap):
            if node_name == "a":
                gate_a.wait(30)

        session._run_custom_check = check
        monkeypatch.setattr(session_mod, "_wait_for_futures", wait_as_the_drain_parks)
        monkeypatch.setattr(session_mod, "_CUSTOM_CHECK_TIMEOUT_S", 30.0)
        session.wrap("a", lambda state: dict(_CLEAN_OUT))({"query": "q"})
        drain = threading.Thread(target=session._apply_deferred_judges)
        drain.start()
        try:
            assert waiting.wait(30), "the drain never parked on run A's check"
            session.begin_new_run()
            session.wrap("b", lambda state: dict(_CLEAN_OUT))({"query": "q"})
            assert len(session._pending_custom_checks) == 1
            with caplog.at_level(logging.WARNING, logger="argus"):
                gate_a.set()  # run A's drain goes on to its pool swap
                drain.join(30)
            assert not drain.is_alive()

            assert len(session._pending_custom_checks) == 1, "run B's check was cleared"
            assert session._pools_closed is False, "run B was closed by run A's drain"
            assert session._check_pool is not None
            session.wrap("c", lambda state: dict(_CLEAN_OUT))({"query": "q"})
            assert len(session._pending_custom_checks) == 2, "run B's next submit was refused"
        finally:
            gate_a.set()
            drain.join(30)
            session.finalize()
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("new run began" in m for m in messages), messages

    @pytest.mark.parametrize(
        "prefix,pool_attr",
        [("argus-check", "_check_pool"), ("argus-judge", "_judge_pool")],
    )
    def test_a_pool_being_built_cannot_outlive_the_swap_that_closes_the_run(
        self, monkeypatch, tmp_path, prefix, pool_attr
    ):
        """The node is mid-way through building its pool when finalize swaps the pools.

        The read of the closed flag, the build and the store are one step under
        the pool lock, and so is the swap. Without that, the drain sees no pool,
        closes the run, and the node then stores the pool it built: a pool that
        no one will ever shut down. Event-driven: the node is parked inside the
        pool's constructor, and the drain is let go only once it has either
        queued for the lock (correct) or run straight through (broken).
        """
        import threading

        import argus.session as session_mod

        session = self._session(monkeypatch, tmp_path)
        building, drain_progressed, release = (threading.Event() for _ in range(3))
        built: list = []
        shut_down: list = []

        class SlowToBuild(session_mod.ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if kwargs.get("thread_name_prefix") == prefix:
                    built.append(self)
                    building.set()
                    release.wait(30)

            def shutdown(self, *args, **kwargs):
                shut_down.append(self)
                return super().shutdown(*args, **kwargs)

        class SpyLock:
            """A lock that says when the drain thread asks for it."""

            def __init__(self):
                self._lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == "drain":
                    drain_progressed.set()  # about to queue behind the node, if it holds it
                self._lock.acquire()

            def __exit__(self, *exc):
                self._lock.release()

        def drain():
            session._apply_deferred_judges()
            drain_progressed.set()  # ran straight through: nothing made it wait

        session._pool_lock = SpyLock()
        monkeypatch.setattr(session_mod, "ThreadPoolExecutor", SlowToBuild)
        node = threading.Thread(
            target=lambda: session.wrap("late", lambda state: dict(_TODO_OUT))({"query": "q"}),
            name="node",
        )
        finalizer = threading.Thread(target=drain, name="drain")
        node.start()
        try:
            assert building.wait(30), "the node never started building its pool"
            finalizer.start()
            assert drain_progressed.wait(30), "the drain never got as far as the swap"
        finally:
            release.set()
            node.join(30)
            if finalizer.ident is not None:
                finalizer.join(30)

        assert not node.is_alive() and not finalizer.is_alive()
        assert len(built) == 1
        assert getattr(session, pool_attr) is None, "the closed run kept a pool nobody shuts down"
        assert built[0] in shut_down
        session.finalize()


def _count_saves(monkeypatch):
    """The run id of every ``save_run`` the session makes (the save still happens)."""
    import argus.session as session_mod

    saves: list[str] = []
    real_save = session_mod.save_run

    def save(record):
        saves.append(record.run_id)
        real_save(record)

    monkeypatch.setattr(session_mod, "save_run", save)
    return saves


class _OnClear(list):
    """A pending list that says when a drain has taken its snapshot of it."""

    def __init__(self):
        import threading

        super().__init__()
        self.cleared = threading.Event()

    def clear(self):
        super().clear()
        self.cleared.set()


@pytest.mark.integration
class TestConcurrentFinalizes:
    """Finalize can run twice at once: a crash on a parallel branch, the last terminal."""

    @pytest.mark.parametrize("kind", ["judge", "check"])
    def test_a_second_finalize_does_not_cancel_what_the_first_drain_waits_for(
        self, monkeypatch, tmp_path, caplog, kind
    ):
        """The second finalize sees an empty snapshot and shuts the same pool down.

        It used to do so with ``cancel_futures=True``, which cancelled the
        queued verdict (or check) the first drain was still waiting for.
        """
        import logging
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from argus.models import LLMInvestigationConfig, SemanticCheckResult
        from argus.session import ArgusSession, _PendingJudge

        session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True)
        )
        session.wrap("n", lambda state: dict(_CLEAN_OUT))({"query": "q"})
        event = session._events[0]
        verdict = SemanticCheckResult(
            passed=True,
            reason="fine",
            confidence=0.9,
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1.0,
        )
        gate = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1)
        pool.submit(gate.wait, 30)  # the pool's only worker: whatever is queued waits for it
        pending = _OnClear()
        if kind == "judge":
            queued = pool.submit(lambda: (verdict, []))
            session._judge_pool = pool
            session._pending_judges = pending
            pending.append(
                _PendingJudge(
                    event=event,
                    future=queued,
                    inspection=event.inspection,
                    validator_results=[],
                    anomaly_signals=[],
                    ambiguous_signals=[],
                    deterministic_status="pass",
                    node_name="n",
                    input_snap={},
                    output_snap=dict(_CLEAN_OUT),
                )
            )
        else:
            queued = pool.submit(lambda: _custom_signal("warning"))
            session._check_pool = pool
            session._pending_custom_checks = pending
            pending.append((event.inspection, "no_pii on n", queued))

        first = threading.Thread(target=session._apply_deferred_judges)
        with caplog.at_level(logging.WARNING, logger="argus"):
            first.start()
            try:
                assert pending.cleared.wait(30), "the first drain never took its snapshot"
                session._finalize()  # the second finalize: an empty snapshot, the same pool
                assert not queued.cancelled(), "the second finalize cancelled the first's future"
            finally:
                gate.set()
                first.join(30)

        assert not first.is_alive()
        if kind == "judge":
            assert event.semantic_check is verdict
        else:
            assert "check:no_pii" in [s.sig_id for s in event.inspection.semantic_signals]
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not [m for m in messages if "lost" in m or "skipped" in m], messages

    def test_a_judge_the_drain_gave_up_on_is_cancelled(self, monkeypatch, tmp_path):
        """What a drain stops waiting for must not start later, and only that."""
        from concurrent.futures import Future
        from concurrent.futures import TimeoutError as FutureTimeout

        from argus.models import LLMInvestigationConfig
        from argus.session import ArgusSession, _PendingJudge

        class OutOfTime(Future):
            def result(self, timeout=None):
                raise FutureTimeout()  # the drain's wait for this verdict ran out

        session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True)
        )
        session.wrap("n", lambda state: dict(_CLEAN_OUT))({"query": "q"})
        event = session._events[0]
        gave_up_on, other = OutOfTime(), Future()
        for future in (gave_up_on, other):
            session._pending_judges.append(
                _PendingJudge(
                    event=event,
                    future=future,
                    inspection=event.inspection,
                    validator_results=[],
                    anomaly_signals=[],
                    ambiguous_signals=[],
                    deterministic_status="pass",
                    node_name="n",
                    input_snap={},
                    output_snap=dict(_CLEAN_OUT),
                )
            )
        other.set_result((None, []))

        session._apply_deferred_judges()

        assert gave_up_on.cancelled()
        assert not other.cancelled() and other.result() == (None, [])
        session.finalize()

    @staticmethod
    def _session_with_a_parked_verdict(gate):
        """One recorded step whose verdict (a failing one) arrives only when ``gate`` is set."""
        from concurrent.futures import ThreadPoolExecutor

        from argus.models import LLMInvestigationConfig, SemanticCheckResult
        from argus.session import ArgusSession, _PendingJudge

        session = ArgusSession(
            llm_investigation=LLMInvestigationConfig(enabled=True, semantic_check=True)
        )
        session.wrap("a", lambda state: dict(_CLEAN_OUT))({"query": "q"})
        event = session._events[0]
        verdict = SemanticCheckResult(
            passed=False,
            reason="bad",
            confidence=0.95,
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1.0,
        )
        pool = ThreadPoolExecutor(max_workers=2)
        session._judge_pool = pool
        session._pending_judges = pending = _OnClear()
        pending.append(
            _PendingJudge(
                event=event,
                future=pool.submit(lambda: (gate.wait(30), (verdict, []))[1]),
                inspection=event.inspection,
                validator_results=[],
                anomaly_signals=[],
                ambiguous_signals=[],
                deterministic_status="pass",
                node_name="a",
                input_snap={},
                output_snap=dict(_CLEAN_OUT),
            )
        )
        return session, event, pending

    def test_overlapping_finalizes_save_one_record_and_it_has_the_verdict(
        self, monkeypatch, tmp_path
    ):
        """A second finalize used to set ``_completed`` and save while the first still drained.

        It saw an empty snapshot (the first had taken the verdict's entry),
        so the saved step said "pass" with no semantic check, while the
        in-memory event got the verdict a moment later. Finalize is now one
        step at a time: the second waits for the first, then finds it done.
        """
        import threading

        from argus.storage import load_run

        gate, progressed = threading.Event(), threading.Event()
        saves = _count_saves(monkeypatch)
        session, event, pending = self._session_with_a_parked_verdict(gate)

        class SpyLock:
            """The finalize lock, plus a signal when the second finalizer asks for it."""

            def __init__(self):
                self._lock = threading.RLock()

            def acquire(self, *args, **kwargs):
                if threading.current_thread().name == "second":
                    progressed.set()  # about to queue behind the first, if it holds the lock
                return self._lock.acquire(*args, **kwargs)

            def release(self):
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()

        session._finalize_lock = SpyLock()

        def second():
            session._finalize()
            progressed.set()  # ran straight through: nothing made it wait

        first = threading.Thread(target=session._finalize, name="first")
        late = threading.Thread(target=second, name="second")
        first.start()
        try:
            assert pending.cleared.wait(30), "the first finalize never took its snapshot"
            late.start()
            assert progressed.wait(30), "the second finalize neither queued nor finished"
        finally:
            gate.set()
            first.join(30)
            if late.ident is not None:
                late.join(30)

        assert not first.is_alive() and not late.is_alive()
        assert saves == [session.run_id], "one finalize saves, once"
        saved = load_run(session.run_id).steps[0]
        assert saved.semantic_check is not None, "the saved step has no verdict"
        assert saved.semantic_check.reason == "bad"
        assert event.semantic_check is not None

    def test_twenty_concurrent_finalizes_finish_and_save_once(self, monkeypatch, tmp_path):
        """Direct finalizes and crash auto-finalizes at once: no deadlock, one record."""
        import threading

        from argus.storage import load_run

        gate, saves = threading.Event(), _count_saves(monkeypatch)
        session, _, _ = self._session_with_a_parked_verdict(gate)
        start = threading.Barrier(21)
        errors: list[BaseException] = []

        def direct():
            try:
                start.wait(30)
                session._finalize()
            except BaseException as exc:
                errors.append(exc)

        def crash(i):
            def boom(state):
                raise RuntimeError(f"boom {i}")

            try:
                start.wait(30)
                session.wrap(f"c{i}", boom)({"query": "q"})  # a crash finalizes the run
            except RuntimeError:
                pass
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=direct) for _ in range(10)]
        threads += [threading.Thread(target=crash, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        try:
            start.wait(30)
            gate.set()  # the verdict arrives while all twenty are finalizing
            for thread in threads:
                thread.join(60)
        finally:
            gate.set()
        assert not [t for t in threads if t.is_alive()], "a finalize deadlocked"
        assert errors == []
        assert saves == [session.run_id]
        assert load_run(session.run_id).steps[0].semantic_check is not None
        assert session._completed

    def test_the_atexit_hook_gives_up_on_a_finalize_that_never_ends(
        self, monkeypatch, tmp_path, caplog
    ):
        """Interpreter exit must not hang behind a daemon thread stuck mid-finalize."""
        import logging
        import threading
        import time

        import argus.session as session_mod
        from argus.models import LLMInvestigationConfig
        from argus.session import ArgusSession

        monkeypatch.setattr(session_mod, "_ATEXIT_FINALIZE_WAIT_S", 0.2)
        session = ArgusSession(llm_investigation=LLMInvestigationConfig(enabled=False))
        session.wrap("a", lambda state: dict(_CLEAN_OUT))({"query": "q"})
        holding, release = threading.Event(), threading.Event()

        def hold():
            with session._finalize_lock:
                holding.set()
                release.wait(30)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert holding.wait(30)
            started = time.monotonic()
            with caplog.at_level(logging.WARNING, logger="argus"):
                session._atexit_finalize()
            elapsed = time.monotonic() - started
        finally:
            release.set()
            holder.join(30)

        assert elapsed < 20, f"the exit hook waited {elapsed:.1f}s on another finalize"
        assert not session._completed
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("atexit" in m and "skipped" in m for m in messages), messages
        session.finalize()
        assert session._completed


# ── a failing custom check is listed, never counted ──────────────────────────

# A heuristic critical failure (``error_response``: nested HTTP 404). The rules
# fail the step; a run reviewer that does not confirm it settles it back to pass.
_NOT_FOUND_OUT = {
    "found": {"status": 404, "message": "Not Found"},
    "answer": "Nothing posted yet today.",
}


_JUDGE_AGREES = {"pass": False, "reason": "agreed", "confidence": 0.9, "failure_kind": "other"}


def _custom_signal(severity):
    return SemanticSignal(
        sig_id="check:no_pii",
        category="custom_check",
        severity=severity,
        description="SSN found",
        field_path=(),
        evidence="leak",
        confidence=0.0,
    )


def _recorded_step(output):
    """One real, unfinalized session step: the inspector has graded it, nothing else has."""
    import atexit

    from argus.models import LLMInvestigationConfig
    from argus.session import ArgusSession

    session = ArgusSession(llm_investigation=LLMInvestigationConfig(enabled=False))
    atexit.unregister(session._atexit_finalize)  # never persist this one at interpreter exit
    session.wrap("summarize", lambda state: dict(output))({"query": "q"})
    return session, session._events[0]


@pytest.mark.integration
class TestFailingCustomCheckIsAdvisoryEverywhere:
    """Each consumer of ``semantic_signals`` reads a custom check as a listing, not evidence."""

    @pytest.mark.parametrize("severity", ["warning", "critical"])
    def test_settle_step_signals_does_not_count_a_custom_check(self, severity):
        from argus.review import Review, settle_step_signals

        _, event = _recorded_step(_NOT_FOUND_OUT)
        assert event.status == "fail"
        event.inspection.semantic_signals.append(_custom_signal(severity))

        settle_step_signals([event], Review(ok=True))

        assert event.status == "pass", "the 404 was demoted; the check did not hold it at fail"
        assert not event.inspection.has_tool_failure
        assert _sig_ids(event) == ["check:no_pii"], "and the check is still on the step"

    def test_a_custom_check_on_the_step_does_not_stop_grading_settling_it(self, tmp_path):
        """Through ``grading.finish``, the call site of ``settle_step_signals``."""
        from argus import grading
        from argus.check import evaluate_run
        from argus.review import Review
        from argus.storage import load_run

        session = grading.new_session(
            ["summarize"],
            {},
            set(),
            {},
            judge=False,
            validators={},
            strict=False,
            max_field_size=50_000,
        )
        session.reviewer = lambda rows, events: Review(ok=True)
        session.wrap("summarize", lambda state: dict(_NOT_FOUND_OUT))({"query": "q"})
        # A check that has already landed when the reviewer settles the step (a
        # replayed trace, or any later reordering of finalize).
        session._events[0].inspection.semantic_signals.append(_custom_signal("critical"))

        grading.finish(session, consumers=None)

        record = load_run(session.run_id)
        assert record.steps[0].status == "pass"
        assert record.overall_status == "clean"
        assert evaluate_run(record).passed is True
        assert "check:no_pii" in _sig_ids(record.steps[0]), "still reported"

    def test_the_reviewer_does_not_promote_a_step_on_a_custom_check_warning(self):
        """``_warning_signal`` is a rule's warning; a check is not a rule."""
        from argus.review import Review, confirm_warnings

        _, event = _recorded_step(_LONG_OUT)
        event.inspection.semantic_signals.append(_custom_signal("warning"))
        item = {"kind": "contradicted", "claim": "c", "why": "w", "correction": "x"}

        confirm_warnings([event], Review(ok=True, verified={"summarize": [item]}))

        assert event.status == "pass"
        assert event.review[0]["role"] == "advisory"

    @pytest.mark.parametrize("severity", ["warning", "critical"])
    def test_origin_summary_is_the_same_with_and_without_a_failing_check(
        self, monkeypatch, tmp_path, severity
    ):
        _install_llm(monkeypatch, check=_LEAK)
        kwargs = {"output": _LONG_OUT, "behavior_type": "retrieval_result"}

        plain, _ = _run_step(tmp_path, specs=[], **kwargs)
        checked, step = _run_step(tmp_path, specs=[{**_PII_SPEC, "severity": severity}], **kwargs)

        assert "check:no_pii" in _sig_ids(step)

        def shown(record):
            return [
                (t.event_type, t.label, t.signal_summary) for t in record.correlation.timeline
            ]

        assert shown(plain) == [
            ("degradation_onset", "ORIGIN: degradation starts at summarize", "anomaly: BA-005")
        ]
        assert shown(checked) == shown(plain)
        assert checked.correlation.causal_summary == plain.correlation.causal_summary

    def test_root_cause_chain_does_not_count_a_custom_check(self):
        """A non-pass step with only an empty field is no origin; a check cannot make it one."""
        from argus.inspector import build_root_cause_chain
        from argus.models import InspectionResult, NodeEvent

        def event(*signals):
            return NodeEvent(
                step_index=0,
                node_name="summarize",
                status="fail",
                input_state={},
                output_dict={"summary": "x"},
                duration_ms=1.0,
                timestamp_utc="2026-01-01T00:00:00+00:00",
                inspection=InspectionResult(
                    is_silent_failure=False,
                    missing_fields=[],
                    empty_fields=["notes"],
                    type_mismatches=[],
                    severity="warning",
                    message="",
                    semantic_signals=list(signals),
                ),
            )

        assert build_root_cause_chain([event()], {}) == []
        assert build_root_cause_chain([event(_custom_signal("critical"))], {}) == []

    def test_argus_fix_writes_no_prompt_for_a_step_flagged_only_by_a_check(
        self, monkeypatch, tmp_path
    ):
        from argus.fix_prompt import FixPromptError, build_fix_prompt_for_record

        _install_llm(monkeypatch, check=_LEAK)
        record, step = _run_step(tmp_path, output=_LONG_OUT)
        assert step.status == "pass" and _sig_ids(step) == ["check:no_pii"]

        with pytest.raises(FixPromptError, match="shows no detected problem"):
            build_fix_prompt_for_record(record, node="summarize")

    @pytest.mark.parametrize("failing", ["tool_failure", "judge_status"])
    def test_a_check_does_not_change_the_fix_prompt_of_a_failing_step(
        self, monkeypatch, tmp_path, failing
    ):
        """Every reader in the prompt: headline, evidence paragraphs, success conditions."""
        import copy

        from argus.fix_prompt import build_fix_prompt_for_record

        _install_llm(monkeypatch, check=_LEAK)
        output = _NOT_FOUND_OUT if failing == "tool_failure" else _LONG_OUT
        record, step = _run_step(
            tmp_path, output=output, specs=[{**_PII_SPEC, "severity": "critical"}]
        )
        if failing == "judge_status":
            # failed by the LLM judge: its only inspection evidence is the check
            step.status = "semantic_fail"
        assert "check:no_pii" in _sig_ids(step)

        plain = copy.deepcopy(record)
        insp = plain.steps[0].inspection
        insp.semantic_signals = [s for s in insp.semantic_signals if s.category != "custom_check"]

        with_check = build_fix_prompt_for_record(record, node="summarize").prompt
        without = build_fix_prompt_for_record(plain, node="summarize").prompt
        assert with_check == without
        assert "SSN found" not in with_check

    def test_signature_stats_do_not_count_a_check_as_a_signature(self, monkeypatch, tmp_path):
        from argus.signature_stats import compute_stats

        _install_llm(monkeypatch, check=_LEAK, coherence=_JUDGE_AGREES)
        _, step = _run_step(tmp_path, output=_TODO_OUT)
        assert {"PH-001", "check:no_pii"} <= set(_sig_ids(step))

        assert set(compute_stats(include_builtins=True)) == {"PH-001"}

    def test_the_investigator_does_not_cite_a_check_as_why_the_judge_failed_a_step(
        self, monkeypatch, tmp_path
    ):
        from argus.llm_investigator import should_investigate
        from argus.models import LLMInvestigationConfig

        _install_llm(monkeypatch, check=_LEAK)
        record, step = _run_step(tmp_path, output=_LONG_OUT)
        step.status = "semantic_fail"
        assert "check:no_pii" in _sig_ids(step)

        _, reasons = should_investigate(record, LLMInvestigationConfig(enabled=True))
        assert reasons == ["semantic failure at 'summarize'"]

    @pytest.mark.parametrize("output", [_TODO_OUT, _LONG_OUT], ids=["with_a_rule_signal", "alone"])
    def test_the_investigator_briefing_is_the_same_with_and_without_a_check(
        self, monkeypatch, tmp_path, output
    ):
        """The briefing steers the investigator's answer and the stored candidate signatures."""
        import copy

        from argus.llm_investigator import compress_intelligence

        _install_llm(monkeypatch, check=_LEAK, coherence=_JUDGE_AGREES)
        record, step = _run_step(tmp_path, output=output)
        assert "check:no_pii" in _sig_ids(step)

        plain = copy.deepcopy(record)
        insp = plain.steps[0].inspection
        insp.semantic_signals = [s for s in insp.semantic_signals if s.category != "custom_check"]

        briefing = compress_intelligence(record)
        assert briefing == compress_intelligence(plain)
        assert "check:no_pii" not in json.dumps(briefing)
        assert "custom_check" not in json.dumps(briefing)

    def test_the_gate_the_cli_payload_and_the_terminal_line_ignore_a_check(
        self, monkeypatch, tmp_path
    ):
        """Guard: nothing downstream of the run file reads a check as a failure."""
        from argus.check import evaluate_run
        from argus.cli.cmd_check import check_payload
        from argus.findings import format_run_finding

        _install_llm(monkeypatch, check=_LEAK)
        record, step = _run_step(
            tmp_path, output=_LONG_OUT, specs=[{**_PII_SPEC, "severity": "critical"}]
        )
        assert "check:no_pii" in _sig_ids(step)

        result = evaluate_run(record)
        assert result.passed is True and result.failing_nodes == () and result.reasons == ()
        payload = check_payload(record)
        assert payload["passed"] is True
        assert payload["failing_nodes"] == [] and payload["reasons"] == []
        assert format_run_finding(record) is None
        assert [f["type"] for f in payload["findings"]] == ["custom_check"], "listed, not hidden"

    def test_check_findings_are_sourced_from_the_llm_not_a_heuristic(self, monkeypatch, tmp_path):
        _install_llm(monkeypatch, check=_LEAK, coherence=_JUDGE_AGREES)
        record, _ = _run_step(tmp_path, output=_TODO_OUT)

        by_type = {f.type: f for f in record.findings}
        assert by_type["custom_check"].source == "llm"
        assert by_type["placeholder_outputs"].source == "heuristic", "rule findings are unchanged"
        assert by_type["custom_check"].id != by_type["placeholder_outputs"].id
