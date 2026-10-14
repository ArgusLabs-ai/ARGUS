"""Structured judge protocol: forced tool-call verdicts + serialized finalize.

No network anywhere — the LLM proxy and the provider HTTP layer are mocked
the same way as tests/test_semantic_checker_unit.py and tests/test_smoke.py.
"""

from __future__ import annotations

import json

import pytest

from argus.models import SemanticSignal
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
        tool = build_verdict_tool("my_tool", "desc", {"x": {"type": "boolean"}}, ["x"])
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
        assert _verdict_from_tool_calls(_tool_call_result('{"pass": tru')) is None

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

    def test_decisive_property_is_listed_first_in_the_tool_schema(self):
        """Property order is a hint to the model, not a guarantee."""
        judge = JUDGE_VERDICT_TOOL["function"]["parameters"]
        assert list(judge["properties"])[0] == "pass"
        assert judge["required"][0] == "pass"

    def test_no_tool_calls_is_no_verdict(self):
        assert (
            _verdict_from_tool_calls({"choices": [{"message": {"content": '{"pass": true}'}}]})
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
        response = _tool_call_result('{"pass": tru', content='{"pass": true, "confidence": 0.7}')
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

        monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
        out = providers.call_google(
            api_key="g-key",
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            tools=[JUDGE_VERDICT_TOOL],
            tool_choice=JUDGE_TOOL_CHOICE,
        )
        body = captured["body"]
        decl = body["tools"][0]["functionDeclarations"][0]
        assert decl["name"] == "record_verdict"
        assert decl["parameters"]["type"] == "OBJECT"
        # Gemini's FunctionDeclaration schema rejects additionalProperties; the
        # judge tool emits it at the top level and inside array items.
        assert "additionalProperties" in json.dumps(JUDGE_VERDICT_TOOL)
        assert len(body["tools"][0]["functionDeclarations"]) == 1
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
        tools = [JUDGE_VERDICT_TOOL]
        body = self._google_body(monkeypatch, tools=tools, tool_choice=JUDGE_TOOL_CHOICE)
        (judge,) = [d["parameters"] for d in body["tools"][0]["functionDeclarations"]]
        judge_props = JUDGE_VERDICT_TOOL["function"]["parameters"]["properties"]
        assert judge["propertyOrdering"] == list(judge_props)
        assert judge["propertyOrdering"][0] == "pass"
        nested = judge["properties"]["disambiguation_verdicts"]["items"]
        assert nested["propertyOrdering"] == ["sig_id", "is_failure", "confidence", "reason"]
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
class _FakeHTTPResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_CLEAN_OUT = {"summary": "Q3 revenue grew 12%."}


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

    def test_a_second_finalize_does_not_cancel_what_the_first_drain_waits_for(
        self, monkeypatch, tmp_path, caplog
    ):
        """The second finalize sees an empty snapshot and shuts the same pool down.

        It used to do so with ``cancel_futures=True``, which cancelled the
        queued verdict the first drain was still waiting for.
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
        assert event.semantic_check is verdict
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
