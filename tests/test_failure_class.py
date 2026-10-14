"""PRD-02 -- deterministic retryability taxonomy.

Covers the pure classifier (statuses, text, word boundaries, pathological
input), the inspector paths that feed it (payload errors with no status, HTTP
status bodies, tool exceptions, strict mode, ``inspect_tool_calls``), the
``ToolFailure`` / ``Finding`` models and their storage round trip (old records
included), the fix-prompt advice lines, and ``recovered_after_transient`` -- unit
level and on a real LangGraph loop. Zero LLM calls anywhere.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_event, make_inspection, make_run_record
from typer.testing import CliRunner

from argus.check import evaluate_run
from argus.cli.main import app
from argus.contextual import GUESS_CONFIDENCE
from argus.failure_class import (
    CLASS_FATAL,
    CLASS_RATE_LIMITED,
    CLASS_TRANSIENT,
    classify_failure_class,
    coerce_status,
    is_rate_limit_text,
    status_from_text,
)
from argus.findings import collect_findings
from argus.fix_prompt import build_fix_prompt_for_record
from argus.grading import _confirmed_findings
from argus.inspector import inspect_tool_calls, inspect_tool_outputs, inspect_transition
from argus.models import Finding, ToolFailure
from argus.recorder import ArgusRecorder
from argus.storage import load_run, save_run

F, T, R = CLASS_FATAL, CLASS_TRANSIENT, CLASS_RATE_LIMITED


def _classes(payload: dict, **kw) -> list[tuple[str, str]]:
    """(failure_type, failure_class) the real inspector assigns to a payload."""
    return [
        (t.failure_type, t.failure_class)
        for t in inspect_tool_outputs(payload, **kw).tool_failures
    ]


# -- the pure classifier ------------------------------------------------------


@pytest.mark.parametrize("failure_type", ["error_response", "error_in_data", "tool_error"])
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (408, T), (425, T), (500, T), (502, T), (503, T), (504, T), (520, T), (599, T),
        (429, R),
        (400, F), (401, F), (403, F), (404, F), (409, F), (422, F), (423, F),
        (501, F), (505, F), (511, F),
        ("503", T), (" 429 ", R), ("404", F),
        (None, F), (200, F), (302, F), (600, F), (99, F), (True, F), ("abc", F), ("5030", F),
        (503.0, F), ([503], F), ({}, F),
    ],
)  # fmt: skip
def test_status_decides_class_for_call_failures(failure_type, status, expected) -> None:
    assert classify_failure_class(failure_type, status) == expected


def test_no_status_and_no_evidence_is_fatal() -> None:
    """The unfixed default was transient: payload errors have no status at all."""
    for failure_type in ("error_response", "error_in_data", "tool_error"):
        assert classify_failure_class(failure_type) == F
        assert classify_failure_class(failure_type, None, None) == F
        assert classify_failure_class(failure_type, text="") == F


def test_explicit_status_beats_text_and_a_non_error_status_defers_to_text() -> None:
    assert classify_failure_class("error_response", 404, "request timed out") == F
    assert classify_failure_class("error_response", 503, "Bad request") == T
    assert classify_failure_class("error_response", 200, "request timed out") == T
    assert classify_failure_class("error_response", None, "HTTP 401 timed out") == F


@pytest.mark.parametrize(
    ("failure_type", "expected"),
    [
        ("rate_limit", R),
        ("empty_result", F),
        ("partial_failure", F),
        ("json_in_string", F),
        ("placeholder_detected", F),
        ("missing_field_guess", F),
        ("review_confirmed", F),
        ("stuck_loop", F),
        ("some_future_type", F),
        ("", F),
    ],
)
def test_fixed_types_ignore_status_and_text(failure_type, expected) -> None:
    assert classify_failure_class(failure_type) == expected
    assert classify_failure_class(failure_type, 503, "request timed out") == expected
    assert classify_failure_class(failure_type, 404) == expected


@pytest.mark.parametrize("bad", [None, 0, 3.5, b"rate_limit", ["rate_limit"], {}, object()])
def test_a_malformed_failure_type_is_fatal_not_an_exception(bad) -> None:
    assert classify_failure_class(bad) == F
    assert classify_failure_class(bad, 503, "timed out") == F


def test_unknown_type_defaults_fatal_and_is_logged_at_debug(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="argus.failure_class")
    assert classify_failure_class("brand_new_type_xyz") == F
    assert "brand_new_type_xyz" in caplog.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # transient: explicit wording that names a timeout / reset / unavailability
        ("TimeoutError('read timed out')", T),
        ("ReadTimeout('')", T),
        ("httpx.ConnectTimeout: timed out", T),
        ("APITimeoutError: Request timed out.", T),
        ("timeout", T),
        ("ETIMEDOUT", T),
        ("ConnectionResetError: [Errno 54] ECONNRESET", T),
        ("ConnectionResetError(54, 'Connection reset by peer')", T),
        ("RemoteProtocolError: Server disconnected without sending a response.", T),
        ("The service is temporarily unavailable", T),
        ("ServiceUnavailableError", T),
        ("overloaded_error: Overloaded", T),
        ("Please try again later", T),
        ("Internal Server Error", T),
        ("Bad Gateway", T),
        ("gateway time-out", T),
        # status in an HTTP context
        ("503 Service Unavailable", T),
        ("HTTP 502", T),
        ("HTTP/1.1 504 Gateway Timeout", T),
        ("Request failed with status code 500", T),
        ("httpx.HTTPStatusError: Server error '503 Service Unavailable' for url 'https://x'", T),
        ("{'code': 503, 'message': 'upstream'}", T),
        ("Error 502: upstream", T),
        # rate limited
        ("RateLimitError: Rate limit reached for gpt-4 on tokens per min", R),
        ("Rate limit exceeded. Please retry after 30 seconds.", R),
        ("rate limited", R),
        ("RateLimitError: 429 Too Many Requests", R),
        ("429 Too Many Requests", R),
        ("Too Many Requests", R),
        ("too_many_requests", R),
        ("rate_limit_exceeded", R),
        ("HTTP 429", R),
        ("Request failed with status code 429", R),
        ("ThrottlingException: Rate exceeded", R),
        ("{'status': 429}", R),
        # fatal: a status that is a client fault, a bug, a config problem, or no evidence
        ("401 Unauthorized: invalid API key", F),
        ("HTTP 404", F),
        ("httpx.HTTPStatusError: Client error '404 Not Found' for url 'https://x'", F),
        ("Bad request: field 'limit' must be int", F),
        ("Not found: customer 991", F),
        ("ValueError('bad id')", F),
        ("KeyError('customer')", F),
        ("ConnectionRefusedError(61, 'Connection refused')", F),
        ("connect refused on port 4290", F),
        ("SSLError: certificate verify failed", F),
        ("Name or service not known", F),
        ("success indicator 'success' is False", F),
        # fatal: the false positives the unanchored regex produced
        ("Order 1429 not found", F),
        ("customer 404 not found", F),
        ("port 503 is already in use", F),
        ("quota exceeded: billing account has no payment method", F),
        ("too many request parameters supplied: 'foo'", F),
        ("accurate limit exceeded", F),
        ("Invalid timeout: parameter 'timeout' must be positive", F),
        ("Bad request: field 'timeout' must be int", F),
        ("runtime out of memory", F),
    ],
)
def test_text_classification(text, expected) -> None:
    assert classify_failure_class("tool_error", text=text) == expected
    assert classify_failure_class("error_response", text=text) == expected


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("HTTP 503", 503),
        ("http/1.1 404 not found", 404),
        ("status code: 429", 429),
        ("status=500", 500),
        ("Client error '404 Not Found'", 404),
        ("401 Unauthorized: invalid API key", 401),
        ("'503 Service Unavailable'", 503),
        ("404", 404),
        ("port 4290", None),
        ("Order 1429 not found", None),
        ("connect refused on port 503", None),
        ("HTTP 5030", None),
        ("error code 20000", None),
        ("a 200 response", None),
        ("", None),
        (None, None),
        (503, None),
    ],
)
def test_status_from_text(text, status) -> None:
    assert status_from_text(text) == status


@pytest.mark.parametrize(
    "text",
    [
        "500 MB file limit exceeded",
        "500 mb limit",
        "502 KB payload too large",
        "404 bytes received, expected 10",
        "500 items required, 3 given",
        "503 rows returned but expected 10",
        "500 chars max",
        "500 tokens requested",
        "500 ms limit exceeded",
        "503 seconds elapsed",
        "500 minutes of audio",
    ],
)
def test_a_leading_quantity_is_not_a_status(text) -> None:
    """``500 MB`` is a size, not HTTP 500: it must not read "retry once"."""
    assert status_from_text(text) is None
    assert classify_failure_class("error_response", text=text) == F
    assert _classes({"error": text}) == [("error_response", F)]


@pytest.mark.parametrize(
    ("text", "status", "expected"),
    [
        ("503 Service Unavailable", 503, T),
        ("429 Too Many Requests", 429, R),
        ("500 Internal Server Error", 500, T),
        ("502 Bad Gateway", 502, T),
        ("504 Gateway Timeout", 504, T),
        ("400 Bad Request", 400, F),
        ("500 Server Error: upstream", 500, T),
        ("500", 500, T),
    ],
)
def test_a_leading_status_with_a_reason_phrase_is_still_a_status(text, status, expected) -> None:
    assert status_from_text(text) == status
    assert classify_failure_class("error_response", text=text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rate limit exceeded", True),
        ("rate limited", True),
        ("HTTP 429", True),
        ("429", True),
        ("Too many requests", True),
        ("connect refused on port 4290", False),
        ("quota exceeded: billing", False),
        ("too many request parameters", False),
        ("Order 1429 not found", False),
        ("401 Unauthorized", False),
        (None, False),
        (429, False),
    ],
)
def test_is_rate_limit_text(text, expected) -> None:
    assert is_rate_limit_text(text) is expected


def test_coerce_status() -> None:
    assert [coerce_status(v) for v in (503, "503", " 404 ", True, 99, 600, "1000", None, 5.0)] == [
        503, 503, 404, None, None, None, None, None, None,
    ]  # fmt: skip


def test_pathological_100kb_inputs_stay_fast() -> None:
    """Every regex is word-bounded and linear; a 100 KB blob must not stall a run."""
    blobs = [
        "a" * 100_000,
        " " * 100_000,
        "429" * 33_000,
        "rate " * 20_000,
        "HTTP " * 20_000,
        "HTTP " + "-" * 100_000 + "503",
        "aA" * 50_000,
        "error " + ":" * 100_000,
        "time" + "_" * 100_000 + "out",
        "[" * 100_000,
        "\n".join(["Traceback line"] * 8_000) + "\nRateLimitError: 429 Too Many Requests",
    ]
    worst = 0.0
    for blob in blobs:
        start = time.perf_counter()
        classify_failure_class("tool_error", text=blob)
        classify_failure_class("error_in_data", text=blob)
        is_rate_limit_text(blob)
        status_from_text(blob)
        worst = max(worst, time.perf_counter() - start)
    assert worst < 1.0, f"slowest 100 KB input took {worst:.3f}s"


def test_only_the_head_and_tail_of_a_huge_blob_are_read() -> None:
    """The scan is bounded: a 100 KB message is read head + tail, not end to end."""
    middle = "x" * 50_000 + " request timed out " + "x" * 50_000
    assert classify_failure_class("tool_error", text=middle) == F
    assert classify_failure_class("tool_error", text="request timed out " + middle) == T
    assert classify_failure_class("tool_error", text=middle + " request timed out") == T


def test_a_traceback_that_ends_in_the_exception_is_still_read() -> None:
    blob = (
        "\n".join(["  File x.py, line 1, in f"] * 5_000)
        + "\nRateLimitError: 429 Too Many Requests"
    )
    assert classify_failure_class("tool_error", text=blob) == R


# -- the inspector: payload errors with no HTTP status ------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "401 Unauthorized: invalid API key"}, [("error_response", F)]),
        ({"error": "Bad request: field 'limit' must be int"}, [("error_response", F)]),
        ({"success": False, "message": "Invalid parameter: email"}, [("error_response", F)]),
        ({"ok": False}, [("error_response", F)]),
        ({"failed": True}, [("error_response", F)]),
        ({"record": "Not found: customer 991"}, [("error_in_data", F)]),
        ({"status": "declined"}, [("error_response", F)]),
        ({"status": 404, "message": "Order 1429 not found"}, [("error_response", F)]),
        ({"error": "connect refused on port 4290"}, [("error_response", F)]),
        (
            {"error": "quota exceeded: billing account has no payment method"},
            [("error_response", F)],
        ),
        ({"error": "too many request parameters supplied: 'foo'"}, [("error_response", F)]),
    ],
)
def test_payload_errors_without_transient_evidence_are_fatal(payload, expected) -> None:
    assert _classes(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "503 Service Unavailable"}, [("error_response", T)]),
        ({"error": "Request timed out"}, [("error_response", T)]),
        ({"error": {"code": 503, "message": "x"}}, [("error_response", T)] * 2),
        ({"error": "Error 502: Bad Gateway"}, [("error_response", T)]),
        ({"status": 503}, [("error_response", T)]),
        ({"status_code": "504"}, [("error_response", T)]),
        ({"status": "unavailable"}, [("error_response", T)]),
        ({"status": "timed_out"}, [("error_response", T)]),
        ({"reply": "Timeout while calling upstream"}, [("error_in_data", T)]),
        ({"reply": "Error: 503 Service Unavailable"}, [("error_in_data", T)]),
        ({"error": "Rate limit exceeded. Please retry after 30 seconds."}, [("rate_limit", R)]),
        ({"error": "429"}, [("rate_limit", R)]),
        ({"error": "HTTP 429 Too Many Requests"}, [("rate_limit", R)]),
        ({"status": 429}, [("rate_limit", R)]),
        ({"nested": {"status_code": 503}}, [("error_response", T)]),
    ],
)
def test_payload_errors_with_explicit_evidence_are_retryable(payload, expected) -> None:
    assert _classes(payload) == expected


def test_the_rate_limit_regex_no_longer_matches_substrings() -> None:
    """``429`` inside ``4290`` / ``1429``, quota/billing and "request parameters"."""
    for text in (
        "connect refused on port 4290",
        "Order 1429 not found",
        "quota exceeded: billing account has no payment method",
        "too many request parameters supplied: 'foo'",
    ):
        failures = inspect_tool_outputs({"error": text}).tool_failures
        assert [t.failure_type for t in failures] == ["error_response"], text
        assert failures[0].severity == "critical", text


@pytest.mark.parametrize(
    "text",
    [
        "api_core.exceptions.ResourceExhausted: 429 Resource has been exhausted (check quota)",
        "ResourceExhausted('429 Resource has been exhausted (e.g. check quota).')",
        "requests.exceptions.HTTPError: 429 Client Error: Unknown Error for url: https://x/v1/items",
        "Got 429 from upstream",
        "Received a 429 from the API",
        "API returned a 429",
        "We hit a 429, backing off",
        "Request failed: 429",
        "Request failed (429)",
    ],
)  # fmt: skip
def test_a_429_in_a_call_sentence_is_still_a_non_gating_rate_limit(text) -> None:
    """These warned (never gated) before the 429 substring match was word-bounded; a real
    429 with no "HTTP" word in front must not turn into a run-failing error_response."""
    assert status_from_text(text) == 429
    assert is_rate_limit_text(text) is True
    insp = inspect_tool_outputs({"error": text})
    assert [(t.failure_type, t.severity, t.failure_class) for t in insp.tool_failures] == [
        ("rate_limit", "warning", R)
    ]
    assert insp.has_tool_failure is False


@pytest.mark.parametrize(
    "text",
    [
        "Item 429 is out of stock",
        "order id 429 not found",
        "Record 429 failed schema validation",
        "Line 429: unexpected token",
        "KeyError: 429",
        "failed 429 of 500 calls",
        "got 429 rows back",
        "received 429 bytes",
    ],
)
def test_a_429_that_is_an_id_a_count_or_a_line_number_is_not_a_rate_limit(text) -> None:
    assert status_from_text(text) is None
    (tf,) = inspect_tool_outputs({"error": text}).tool_failures
    assert (tf.failure_type, tf.severity, tf.failure_class) == ("error_response", "critical", F)


@pytest.mark.parametrize(
    "text",
    [
        "Failed (429) records skipped",
        "got 429 messages in queue, nothing processed",
        "failed: 429 retries left, giving up",
        "got an 429 id",
        "got 500 errors",
        "got 500 new messages",
        "got 500 requests",
        "received 500 emails",
        "hit a 500 limit",
        "hit a 503 limit",
        "got 500 limits",
        "got 500 news articles",  # "news" is skipped like an adjective: a count
        "failed (500) rows",
        "error (500) rows skipped",
        "(429) records failed validation",
        "Request failed (503) Services unavailable",  # pinned: a plural noun reads as a count
    ],
)
def test_a_count_after_a_call_verb_is_not_a_status(text) -> None:
    """A count read as HTTP 429 turned a critical error into a non-gating warning (fail-open),
    and read as 5xx it turned fatal into "retry once"."""
    assert status_from_text(text) is None
    assert is_rate_limit_text(text) is False
    assert classify_failure_class("error_response", text=text) == F
    insp = inspect_tool_outputs({"error": text})
    assert [(t.failure_type, t.severity, t.failure_class) for t in insp.tool_failures] == [
        ("error_response", "critical", F)
    ]
    assert insp.has_tool_failure is True


@pytest.mark.parametrize(
    ("text", "status", "expected"),
    [
        ("got a 429 from the API", 429, R),
        ("got 429 Too Many Requests", 429, R),
        ("got a 429 status code", 429, R),
        ("We hit a 429, backing off", 429, R),
        ("hit a 429 rate limit", 429, R),
        ("Request failed (429)", 429, R),
        ("Request failed: 429 Too Many Requests", 429, R),
        ("ResourceExhausted: 429 Quota exceeded for quota metric", 429, R),
        ("HTTPError: 429", 429, R),
        ("HTTP 429", 429, R),
        ("Error code: 429 - {'error': {'message': 'Rate limit reached'}}", 429, R),
        ("429 Too Many Requests", 429, R),
        ("returned 503", 503, T),
        ("got a 500 error", 500, T),
        ("got a 503 while calling the API", 503, T),
        ("Request failed (503) Service Unavailable", 503, T),
        ("503 Service Unavailable", 503, T),
        ("500 Internal Server Error", 500, T),
        ("502 Bad Gateway", 502, T),
        ("504 Gateway Timeout", 504, T),
    ],
)
def test_a_status_after_a_call_verb_is_still_a_status(text, status, expected) -> None:
    assert status_from_text(text) == status
    assert classify_failure_class("error_response", text=text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "hit a 429 limit exceeded",
        "got 429 limit reached, retry later",
        "got 429 limits",
        "hit a 429 limit",
        "failed (429) limit exceeded",
    ],
)
def test_a_429_limit_after_a_call_verb_is_a_rate_limit_not_a_count(text) -> None:
    """A 429 limit is a rate limit; only a 5xx "500 limit" is a count (pinned above)."""
    assert status_from_text(text) == 429
    assert classify_failure_class("error_response", text=text) == R
    insp = inspect_tool_outputs({"error": text})
    assert [(t.failure_type, t.severity) for t in insp.tool_failures] == [
        ("rate_limit", "warning")
    ]
    assert insp.has_tool_failure is False


@pytest.mark.parametrize(
    "word",
    "during despite including via per under across over into about even using upon through "
    "throughout between among against within without toward towards onto along around beyond "
    "behind below above near besides that which where though although unless whereas".split(),
)
def test_a_preposition_or_connective_is_not_a_skipped_adjective(word) -> None:
    """``got 503 during deploys``: only an adjective may sit between the number and the count
    noun, so a function word before a plural leaves the number a status."""
    assert status_from_text(f"got 503 {word} retries") == 503
    assert status_from_text(f"got 429 {word} retries") == 429


@pytest.mark.parametrize(
    ("text", "status", "expected"),
    [
        ("got 429 during retries", 429, R),
        ("got 503 during deploys", 503, T),
        ("got 429 despite retries", 429, R),
        ("returned 500 across shards", 500, T),
    ],
)
def test_a_status_followed_by_a_preposition_and_a_plural_is_still_a_status(
    text, status, expected
) -> None:
    assert status_from_text(text) == status
    assert classify_failure_class("error_response", text=text) == expected


@pytest.mark.parametrize(
    "word",
    "news lens series species always sometimes perhaps afterwards towards besides whereas means "
    "does says seems this its yes various status access success process address class bus "
    "analysis".split(),
)
def test_a_word_that_only_looks_like_a_plural_is_not_a_count(word) -> None:
    """Consonant + ``s`` is not a plural for ``news`` / ``lens`` / ``always`` / ``series``; the
    ``ss`` / ``us`` / ``is`` endings and the 3-letter words never were."""
    assert status_from_text(f"got 503 {word}") == 503
    assert classify_failure_class("error_response", text=f"got 503 {word}") == T


@pytest.mark.parametrize(
    "text",
    [
        "got 429 responses from the API",
        "got 429 responses",
        "API returned 500 errors",
        "API returned 500 errors from upstream",
        "got 503 errors",
        "received 500 responses",
    ],
)
def test_a_plural_after_a_call_verb_is_a_count_even_for_responses_and_errors(text) -> None:
    """DELIBERATE, documented in docs/STATUS.md: ``errors`` and ``responses`` are plural nouns
    like any other, so the number is a count. Fail closed: the failure still gates; only the
    retry class (or the rate-limit warning) is lost."""
    assert status_from_text(text) is None
    assert is_rate_limit_text(text) is False
    assert classify_failure_class("error_response", text=text) == F
    insp = inspect_tool_outputs({"error": text})
    assert [(t.failure_type, t.severity, t.failure_class) for t in insp.tool_failures] == [
        ("error_response", "critical", F)
    ]
    assert insp.has_tool_failure is True


@pytest.mark.parametrize("text", ["429 seconds", "429 sec", "429 secs", "429 s left"])
def test_a_leading_429_before_a_time_unit_is_still_rate_limited(text) -> None:
    """Only the 5xx ``503 seconds elapsed`` is a duration; the 429 verdict is unchanged."""
    assert status_from_text(text) == 429
    assert classify_failure_class("error_response", text=text) == R
    insp = inspect_tool_outputs({"error": text})
    assert [(t.failure_type, t.severity) for t in insp.tool_failures] == [
        ("rate_limit", "warning")
    ]
    assert insp.has_tool_failure is False


@pytest.mark.parametrize(
    "text", ["500 s elapsed", "500 secs", "503 seconds", "429 ms limit exceeded"]
)
def test_a_5xx_or_a_millisecond_quantity_is_still_not_a_status(text) -> None:
    assert status_from_text(text) is None


def test_a_quantity_after_a_status_context_word_is_not_a_status() -> None:
    assert status_from_text("API returned 500 items") is None
    assert status_from_text("API returned 500") == 500
    assert status_from_text("API returned a 500") == 500


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # intended verdict changes against the pre-PRD-02 substring match (docs/STATUS.md)
        ("quota exceeded", ("error_response", "critical")),
        ("RESOURCE_EXHAUSTED: Quota exceeded for quota metric", ("error_response", "critical")),
        ("Order 1429 not found", ("error_response", "critical")),
        ("Request failed on port 4290", ("error_response", "critical")),
        ("Item 429 is out of stock", ("error_response", "critical")),
        ("429 MB file limit exceeded", ("error_response", "critical")),
        ("accurate limit exceeded", ("error_response", "critical")),
        ("too many request parameters supplied: 'foo'", ("error_response", "critical")),
        ("Too Many Request", ("error_response", "critical")),
        ("got 429 responses", ("error_response", "critical")),
        ("ThrottlingException: Rate exceeded", ("rate_limit", "warning")),
        ("Requests are being throttled", ("rate_limit", "warning")),
        # unchanged: real rate limits still warn and do not gate
        ("rate limit exceeded", ("rate_limit", "warning")),
        ("Too Many Requests", ("rate_limit", "warning")),
        ("429 Too Many Requests", ("rate_limit", "warning")),
        ("HTTP 429", ("rate_limit", "warning")),
        ("ratelimited", ("rate_limit", "warning")),
        ("429 secs", ("rate_limit", "warning")),
        ("got 429 limits", ("rate_limit", "warning")),
    ],
)
def test_the_deliberate_rate_limit_verdict_changes_are_pinned(text, expected) -> None:
    (tf,) = inspect_tool_outputs({"error": text}).tool_failures
    assert (tf.failure_type, tf.severity) == expected


def test_a_real_rate_limit_still_warns_and_does_not_gate() -> None:
    insp = inspect_tool_outputs({"error": "rate limit exceeded"})
    assert [(t.failure_type, t.severity) for t in insp.tool_failures] == [
        ("rate_limit", "warning")
    ]
    assert insp.has_tool_failure is False and insp.has_tool_warnings is True


def test_strict_mode_keeps_the_class_when_it_promotes_a_warning() -> None:
    (tf,) = inspect_tool_outputs({"error": "rate limit exceeded"}, strict=True).tool_failures
    assert (tf.severity, tf.failure_type, tf.failure_class) == ("critical", "rate_limit", R)
    (tf,) = inspect_tool_outputs({"reply": "Timeout while calling x"}, strict=True).tool_failures
    assert (tf.severity, tf.failure_type, tf.failure_class) == ("critical", "error_in_data", T)
    (tf,) = inspect_tool_outputs({"reply": "Error: 404 not found"}, strict=True).tool_failures
    assert (tf.severity, tf.failure_class) == ("critical", F)


def test_a_node_s_own_error_is_classified_too() -> None:
    """``inspect_transition`` is the node-output path (own_output=True)."""
    insp = inspect_transition(
        current_node="fetch",
        output_dict={"error": "503 Service Unavailable"},
        merged_state={"error": "503 Service Unavailable"},
        successor_fns=[],
    )
    assert [(t.failure_type, t.failure_class) for t in insp.tool_failures] == [
        ("error_response", T)
    ]


# -- the inspector: recorded tool I/O (inspect_tool_calls) --------------------


def test_inspect_tool_calls_keeps_the_status_aware_class() -> None:
    """The rebuild used to drop failure_class: a 404 re-derived to transient."""
    out = inspect_tool_calls([{"name": "lookup", "output": {"status": 404, "message": "no id"}}])
    assert [(t.failure_type, t.failure_class) for t in out] == [("error_response", F)]
    out = inspect_tool_calls([{"name": "lookup", "output": {"status": 503}}])
    assert [t.failure_class for t in out] == [T]
    out = inspect_tool_calls([{"name": "lookup", "output": {"status": 429}}])
    assert [(t.failure_type, t.failure_class) for t in out] == [("rate_limit", R)]
    out = inspect_tool_calls([{"name": "lookup", "output": '{"error": "401 Unauthorized"}'}])
    assert [t.failure_class for t in out] == [F]
    # evidence that lives in the payload text (no status key) survives the rebuild too
    out = inspect_tool_calls([{"name": "lookup", "output": {"error": "503 Service Unavailable"}}])
    assert [t.failure_class for t in out] == [T]
    out = inspect_tool_calls([{"name": "lookup", "output": {"reply": "Timeout while calling x"}}])
    assert [(t.failure_type, t.failure_class) for t in out] == [("error_in_data", T)]


def test_inspect_tool_calls_strict_keeps_the_class() -> None:
    out = inspect_tool_calls([{"name": "lookup", "output": {"status": 404}}], strict=True)
    assert [(t.severity, t.failure_class) for t in out] == [("critical", F)]
    out = inspect_tool_calls([{"name": "f", "output": {"error": "rate limit"}}], strict=True)
    assert [(t.severity, t.failure_class) for t in out] == [("critical", R)]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("TimeoutError('read timed out')", T),
        ("ReadTimeout('')", T),
        ("RateLimitError: 429 Too Many Requests", R),
        ("RateLimitError('Rate limit reached for gpt-4')", R),
        ("ConnectionResetError: [Errno 54] ECONNRESET", T),
        ("HTTPStatusError(\"Server error '503 Service Unavailable' for url 'https://x'\")", T),
        ("HTTPStatusError(\"Client error '404 Not Found' for url 'https://x'\")", F),
        ("ConnectionRefusedError(61, 'Connection refused')", F),
        ("ValueError('bad id')", F),
        ("KeyError('customer')", F),
        ("Exception('connect refused on port 4290')", F),
        ("Exception('quota exceeded: billing account has no payment method')", F),
    ],
)
def test_tool_exceptions_are_classified_from_the_exception(error, expected) -> None:
    (tf,) = inspect_tool_calls([{"name": "fetch", "error": error}])
    assert (tf.failure_type, tf.severity, tf.failure_class) == ("tool_error", "critical", expected)


# -- ToolFailure / Finding models ----------------------------------------------


def _tf(failure_type="error_response", evidence="e", **kw) -> ToolFailure:
    return ToolFailure(
        failure_type=failure_type, field_name="data", severity="critical", evidence=evidence, **kw
    )


def test_tool_failure_derives_a_class_when_none_is_given() -> None:
    assert _tf("rate_limit", "HTTP 429").failure_class == R
    assert _tf("empty_result").failure_class == F
    assert _tf("error_response", "HTTP 503 error response").failure_class == T
    assert _tf("error_response", "HTTP 404 error response").failure_class == F
    assert _tf("error_response", "nested HTTP 429 rate limit").failure_class == R
    assert _tf("error_response", "success indicator 'ok' is False").failure_class == F


def test_tool_failure_explicit_class_wins_and_garbage_is_rederived() -> None:
    assert _tf("error_response", "HTTP 404", failure_class=T).failure_class == T
    assert _tf("error_response", "HTTP 503", failure_class="").failure_class == T
    assert _tf("error_response", "HTTP 503", failure_class="retryable").failure_class == T
    assert _tf("error_response", "HTTP 404", failure_class=["x"]).failure_class == F  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("Read timed out", T),
        ("Connection reset by peer", T),
        ("Service Unavailable", T),
        ("error field set: 'Request timed out'", T),
        ("Rate limit reached for gpt-4", R),
        ("Bad request", F),
        ("Invalid timeout: parameter 'timeout' must be positive", F),
    ],
)
def test_tool_failure_derives_the_class_from_the_evidence_wording_too(evidence, expected) -> None:
    """A ToolFailure built with no class (a pre-PRD-02 record, a direct call) read only the
    HTTP status, so "Read timed out" was filed fatal."""
    for failure_type in ("error_response", "error_in_data", "tool_error"):
        assert _tf(failure_type, evidence).failure_class == expected, failure_type
    other = F if expected != F else T
    assert _tf("error_response", evidence, failure_class=other).failure_class == other
    assert _tf("empty_result", evidence).failure_class == F  # fixed types ignore the text


@pytest.mark.parametrize("failure_type", ["missing_field_guess", "brand_new_type", "", None, 7])
def test_tool_failure_never_raises_on_an_unknown_type(failure_type) -> None:
    tf = ToolFailure(failure_type, "f", "warning", "an unconfirmed guess")  # type: ignore[arg-type]
    assert tf.failure_class == F


def test_an_unconfirmed_field_guess_is_filed_without_crashing() -> None:
    """grading builds ToolFailure("missing_field_guess") for every guess the run
    reviewer did not confirm; it raised ValueError from __post_init__."""
    guess = Finding(
        id="g1",
        node="writer",
        type="missing_field",
        severity="critical",
        reason="Field `answer` was never written",
        source="heuristic",
        field_path="answer",
        confidence=GUESS_CONFIDENCE,
    )
    event = make_event(node_name="writer", status="pass", inspection=make_inspection())
    session = SimpleNamespace(_events=[event])
    review = SimpleNamespace(ok=True, confirms=lambda node: False)
    assert _confirmed_findings(session, [guess], review, None) == []
    (tf,) = event.inspection.tool_failures
    assert (tf.failure_type, tf.severity, tf.failure_class) == (
        "missing_field_guess",
        "warning",
        F,
    )


def test_collect_findings_carries_the_class_of_each_tool_failure() -> None:
    fails = [
        _tf("rate_limit", "HTTP 429 rate limit"),
        _tf("error_response", "HTTP 503 error response", failure_class=T),
    ]
    fails[1].field_name = "other"
    event = make_event(
        node_name="fetch", status="fail", inspection=make_inspection(tool_failures=fails)
    )
    found = {f.type: f for f in collect_findings([event])}
    assert found["rate_limit"].failure_class == R
    assert found["error_response"].failure_class == T


def test_non_tool_failure_findings_have_no_class() -> None:
    event = make_event(node_name="a", status="fail", inspection=make_inspection(missing=["x"]))
    findings = collect_findings([event])
    assert findings and all(f.failure_class is None for f in findings)


def _save_and_reload(record):
    save_run(record)
    return load_run(record.run_id)


def test_failure_class_round_trips_through_storage() -> None:
    event = make_event(
        node_name="fetch",
        status="fail",
        inspection=make_inspection(
            tool_failures=[_tf("error_response", "HTTP 404", failure_class=T)],
            has_tool_failure=True,
            severity="critical",
        ),
    )
    record = make_run_record(events=[event], status="silent_failure", run_id="rt-new")
    record.findings = collect_findings(record.steps)
    loaded = _save_and_reload(record)
    assert loaded.steps[0].inspection.tool_failures[0].failure_class == T
    assert [f.failure_class for f in loaded.findings] == [T]


def _rewrite(run_id: str, edit) -> None:
    path = Path(".argus") / "runs" / f"{run_id}.json"
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))


def test_records_saved_before_the_field_existed_load_with_a_derived_class() -> None:
    event = make_event(
        node_name="fetch",
        status="fail",
        inspection=make_inspection(
            tool_failures=[
                _tf("rate_limit", "HTTP 429 rate limit"),
                _tf("error_response", "HTTP 503 error response"),
                _tf("empty_result", "tool returned no results"),
            ],
            has_tool_failure=True,
            severity="critical",
        ),
    )
    record = make_run_record(events=[event], status="silent_failure", run_id="rt-old")
    record.findings = collect_findings(record.steps)
    save_run(record)

    def strip(data):
        for step in data["steps"]:
            for tf in step["inspection"]["tool_failures"]:
                tf.pop("failure_class")
        for f in data["findings"]:
            f.pop("failure_class")

    _rewrite("rt-old", strip)
    loaded = load_run("rt-old")
    got = [t.failure_class for t in loaded.steps[0].inspection.tool_failures]
    assert got == [R, T, F]
    assert [f.failure_class for f in loaded.findings] == [None, None, None]


def test_old_records_with_transient_wording_load_as_transient() -> None:
    """The load path is ``ToolFailure(**tf)``: records written before the class existed carry
    only the evidence text, and its wording (not just an HTTP status) must decide the class."""
    wording = [
        "error field set: 'Read timed out'",
        "error field set: 'Connection reset by peer'",
        "nested error field: 'Service Unavailable'",
    ]
    event = make_event(
        node_name="fetch",
        status="fail",
        inspection=make_inspection(
            tool_failures=[_tf("error_response", e, failure_class=F) for e in wording],
        ),
    )
    save_run(make_run_record(events=[event], run_id="rt-wording"))

    def strip(data):
        for tf in data["steps"][0]["inspection"]["tool_failures"]:
            tf.pop("failure_class")

    _rewrite("rt-wording", strip)
    loaded = load_run("rt-wording")
    assert [t.failure_class for t in loaded.steps[0].inspection.tool_failures] == [T, T, T]


def test_a_record_with_an_unknown_failure_type_or_class_still_loads() -> None:
    """Records written by the updated stack carry missing_field_guess; a future
    version may carry a class this one has never heard of."""
    event = make_event(
        node_name="fetch",
        status="fail",
        inspection=make_inspection(
            tool_failures=[_tf("missing_field_guess", "a guess"), _tf("empty_result")],
        ),
    )
    record = make_run_record(events=[event], run_id="rt-weird")
    record.findings = collect_findings(record.steps)
    save_run(record)

    def corrupt(data):
        tfs = data["steps"][0]["inspection"]["tool_failures"]
        tfs[0]["failure_type"] = "a_type_from_the_future"
        tfs[0]["failure_class"] = "retry_with_jitter"
        tfs[1].pop("failure_class")
        data["findings"][0]["failure_class"] = "retry_with_jitter"
        data["findings"][1]["failure_class"] = ["nope"]

    _rewrite("rt-weird", corrupt)
    loaded = load_run("rt-weird")
    assert [t.failure_class for t in loaded.steps[0].inspection.tool_failures] == [F, F]
    assert [f.failure_class for f in loaded.findings] == [None, None]


# -- fix-prompt advice ----------------------------------------------------------


def _prompt_for(payload=None, tool_failures=None) -> str:
    tfs = (
        tool_failures if tool_failures is not None else inspect_tool_outputs(payload).tool_failures
    )
    event = make_event(
        node_name="fetch",
        status="fail",
        inspection=make_inspection(
            tool_failures=tfs,
            has_tool_failure=any(t.severity == "critical" for t in tfs),
            severity="critical",
        ),
    )
    record = make_run_record(events=[event], status="silent_failure")
    record.first_failure_step = "fetch"
    return build_fix_prompt_for_record(record).prompt


def test_fix_prompt_says_retry_once_only_for_explicit_transient_evidence() -> None:
    prompt = _prompt_for({"error": "503 Service Unavailable"})
    assert "Retry guidance: retry once before debugging." in prompt
    prompt = _prompt_for({"error": "Request timed out"})
    assert "retry once before debugging" in prompt


def test_fix_prompt_does_not_tell_you_to_retry_a_payload_level_client_error() -> None:
    """The unfixed default sent a 401 / bad-request payload to "retry once"."""
    for payload in (
        {"error": "401 Unauthorized: invalid API key"},
        {"error": "Bad request: field 'limit' must be int"},
        {"success": False, "message": "Invalid parameter: email"},
        {"status": 404, "message": "no such id"},
    ):
        prompt = _prompt_for(payload)
        assert "retry once before debugging" not in prompt, payload
        assert "Retry guidance: nothing suggests a retry would help; fix the call." in prompt


def test_fix_prompt_advice_for_a_rate_limit_and_class_names_stay_out() -> None:
    prompt = _prompt_for({"error": "rate limit exceeded"})
    assert "Retry guidance: re-queue with backoff; no code change expected." in prompt
    assert "the external call was rate-limited" in prompt  # the per-type line is untouched
    for jargon in ("rate_limited", "transient", "failure_class"):
        assert jargon not in prompt


def test_fix_prompt_has_no_retry_advice_for_content_quality_failures() -> None:
    quality = ToolFailure("placeholder_detected", "answer", "critical", "[PH-001] a placeholder")
    assert "Retry guidance" not in _prompt_for(tool_failures=[quality])


def test_fix_prompt_advice_for_a_swallowed_tool_exception() -> None:
    (tf,) = inspect_tool_calls([{"name": "fetch", "error": "TimeoutError('read timed out')"}])
    assert "Retry guidance: retry once before debugging." in _prompt_for(tool_failures=[tf])


# -- recovered_after_transient ---------------------------------------------------


def _rl(**kw) -> ToolFailure:
    return ToolFailure("rate_limit", "data", "warning", "HTTP 429 rate limit", **kw)


def _steps(*specs) -> list:
    """(status, tool_failures-or-None) per step of one node `fetcher`."""
    return [
        make_event(
            node_name="fetcher",
            status=status,
            step_index=i,
            inspection=make_inspection(tool_failures=tfs) if tfs else None,
        )
        for i, (status, tfs) in enumerate(specs)
    ]


def _recovered(steps):
    return [f for f in collect_findings(steps) if f.type == "recovered_after_transient"]


def test_two_rate_limits_on_retried_steps_then_a_pass_is_reported() -> None:
    (f,) = _recovered(_steps(("retried", [_rl()]), ("retried", [_rl()]), ("pass", None)))
    assert (f.node, f.origin_node, f.severity, f.source) == (
        "fetcher",
        "fetcher",
        "info",
        "heuristic",
    )
    assert "`fetcher` absorbed 2 transient failures" in f.reason


def test_tool_exceptions_retried_twice_then_passing_fire() -> None:
    """429 / timeout exceptions were all `tool_error` -> fatal, so this never fired."""
    exc = inspect_tool_calls([{"name": "fetch", "error": "RateLimitError: 429 Too Many Requests"}])
    slow = inspect_tool_calls([{"name": "fetch", "error": "TimeoutError('read timed out')"}])
    (f,) = _recovered(_steps(("retried", exc), ("retried", slow), ("pass", None)))
    assert f.node == "fetcher"


def test_retries_are_required_not_just_failures() -> None:
    """Two *passing* steps that kept a failure payload are not a recovery."""
    kept = inspect_tool_outputs({"error": "rate limit exceeded"}).tool_failures
    assert _recovered(_steps(("pass", kept), ("pass", kept), ("pass", None))) == []
    # not even when the kept failures are transient
    five_hundred = inspect_tool_outputs({"status": 503}).tool_failures
    assert _recovered(_steps(("pass", five_hundred), ("pass", five_hundred), ("pass", None))) == []


def test_fatal_failures_on_retried_steps_are_not_a_flaky_dependency() -> None:
    for payload in (
        {"error": "401 Unauthorized: invalid API key"},
        {"error": "connect refused on port 4290"},
        {"error": "quota exceeded: billing account has no payment method"},
        {"status": 404},
    ):
        fatal = inspect_tool_outputs(payload).tool_failures
        assert _recovered(_steps(("retried", fatal), ("retried", fatal), ("pass", None))) == []


def test_no_recovery_when_the_node_ends_failed_and_a_single_blip_is_noise() -> None:
    assert _recovered(_steps(("retried", [_rl()]), ("retried", [_rl()]), ("fail", None))) == []
    assert _recovered(_steps(("retried", [_rl()]), ("pass", None))) == []
    assert _recovered(_steps(("retried", [_rl()]), ("retried", [_rl()]), ("crashed", None))) == []


def test_a_crashed_step_among_the_kept_steps_is_not_a_recovery() -> None:
    """A crashed step is kept (a data-accumulator write), so the run fails; the node's LAST
    step passing must not make the info finding say the run is clean."""
    crashed_between = _steps(
        ("retried", [_rl()]), ("crashed", None), ("retried", [_rl()]), ("pass", None)
    )
    assert _recovered(crashed_between) == []
    failed_between = _steps(
        ("retried", [_rl()]), ("retried", [_rl()]), ("fail", None), ("pass", None)
    )
    assert _recovered(failed_between) == []
    # every kept step passing is still a recovery, however many there are
    two_passes = _steps(("retried", [_rl()]), ("pass", None), ("retried", [_rl()]), ("pass", None))
    assert [f.node for f in _recovered(two_passes)] == ["fetcher"]


def test_two_failures_in_one_retried_step_count_as_two() -> None:
    assert len(_recovered(_steps(("retried", [_rl(), _rl()]), ("pass", None)))) == 1


def test_recovered_finding_is_additive_and_has_a_stable_id_per_node() -> None:
    steps = []
    for node in ("a", "b"):
        for i, status in enumerate(("retried", "retried", "pass")):
            steps.append(
                make_event(
                    node_name=node,
                    status=status,
                    step_index=len(steps),
                    inspection=make_inspection(tool_failures=[_rl()]) if i < 2 else None,
                )
            )
    found = _recovered(steps)
    assert {f.node for f in found} == {"a", "b"} and len({f.id for f in found}) == 2
    assert {f.id for f in _recovered(steps)} == {f.id for f in found}
    assert not [f for f in collect_findings(steps) if f.type != "recovered_after_transient"]


def test_a_clean_run_has_no_recovered_finding() -> None:
    assert _recovered([make_event(node_name="a", status="pass")]) == []


def test_recovered_finding_does_not_change_status_or_exit_code(tmp_path) -> None:
    steps = _steps(("retried", [_rl()]), ("retried", [_rl()]), ("pass", None))
    record = make_run_record(events=steps, status="clean", run_id="flaky-1")
    record.findings = collect_findings(record.steps)
    save_run(record)
    assert [f.type for f in record.findings] == ["recovered_after_transient"]
    assert evaluate_run(record).passed is True

    runner = CliRunner()
    text = runner.invoke(app, ["check", "flaky-1"])
    assert text.exit_code == 0
    assert "clean" in text.output and "pass" in text.output
    assert "absorbed 2 transient failures" in text.output and "fetcher" in text.output

    as_json = runner.invoke(app, ["check", "flaky-1", "--format", "json"])
    assert as_json.exit_code == 0
    payload = json.loads(as_json.output)
    assert (payload["overall_status"], payload["passed"]) == ("clean", True)
    assert [f["type"] for f in payload["findings"]] == ["recovered_after_transient"]
    assert payload["findings"][0]["failure_class"] is None

    strict = runner.invoke(app, ["check", "flaky-1", "--strict", "warn_as_fail"])
    assert strict.exit_code == 0


def test_check_escapes_markup_in_a_node_name(tmp_path) -> None:
    steps = [
        make_event(
            node_name="[bold]fetch[/",
            status=status,
            step_index=i,
            inspection=make_inspection(tool_failures=[_rl()]) if status == "retried" else None,
        )
        for i, status in enumerate(("retried", "retried", "pass"))
    ]
    record = make_run_record(events=steps, status="clean", run_id="flaky-markup")
    record.findings = collect_findings(record.steps)
    save_run(record)
    result = CliRunner().invoke(app, ["check", "flaky-markup"])
    assert result.exit_code == 0, result.output
    assert "[bold]fetch[/" in result.output


def test_json_check_shows_the_class_of_a_failing_finding() -> None:
    tf = _tf("error_response", "HTTP 503 error response", failure_class=T)
    event = make_event(
        node_name="fetch",
        status="fail",
        inspection=make_inspection(tool_failures=[tf], has_tool_failure=True, severity="critical"),
    )
    record = make_run_record(events=[event], status="silent_failure", run_id="cls-json")
    record.findings = collect_findings(record.steps)
    save_run(record)
    result = CliRunner().invoke(app, ["check", "cls-json", "--format", "json"])
    assert result.exit_code == 1
    assert [f["failure_class"] for f in json.loads(result.output)["findings"]] == [T]


# -- end to end on a real LangGraph loop ----------------------------------------

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from typing import TypedDict  # noqa: E402

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402


class _S(TypedDict, total=False):
    attempt: int
    result: str


def _loop_app(raises, fail_times: int):
    """`fetch` calls a tool that raises `raises` `fail_times` times, swallows it, loops."""

    @tool
    def flaky_api(attempt: int) -> str:
        """Call the upstream API."""
        if attempt <= fail_times:
            raise raises
        return "42 widgets"

    def fetch(state):
        attempt = state.get("attempt", 0) + 1
        try:
            return {"attempt": attempt, "result": flaky_api.invoke({"attempt": attempt})}
        except Exception:
            return {"attempt": attempt, "result": ""}

    def route(state):
        return "fetch" if not state.get("result") else "done"

    def done(state):
        return {"result": state["result"]}

    g = StateGraph(_S)
    g.add_node("fetch", fetch)
    g.add_node("done", done)
    g.add_edge(START, "fetch")
    g.add_conditional_edges("fetch", route, {"fetch": "fetch", "done": "done"})
    g.add_edge("done", END)
    return g.compile()


def _run_loop(graph):
    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(graph).invoke({})
    return load_run(recorder.session.run_id)


@pytest.mark.parametrize(
    ("raises", "expected_class"),
    [
        (TimeoutError("read timed out"), T),
        (RuntimeError("RateLimitError: 429 Too Many Requests"), R),
    ],
)
def test_real_loop_two_transient_tool_errors_then_success_is_reported(
    raises, expected_class
) -> None:
    record = _run_loop(_loop_app(raises, fail_times=2))
    fetches = [e for e in record.steps if e.node_name == "fetch"]
    assert [e.status for e in fetches] == ["retried", "retried", "pass"]
    raised = [
        tf.failure_class
        for e in fetches[:2]
        for tf in e.inspection.tool_failures
        if tf.failure_type == "tool_error"
    ]
    assert raised == [expected_class, expected_class]
    assert record.overall_status == "clean"
    assert evaluate_run(record).passed is True
    (f,) = [f for f in record.findings if f.type == "recovered_after_transient"]
    assert f.node == "fetch" and f.severity == "info"
    assert "absorbed 2 transient failures" in f.reason
    result = CliRunner().invoke(app, ["check", record.run_id])
    assert result.exit_code == 0 and "absorbed 2 transient failures" in result.output


def test_real_loop_with_a_fatal_tool_error_is_not_a_flaky_dependency() -> None:
    record = _run_loop(_loop_app(PermissionError("401 Unauthorized: invalid API key"), 2))
    fetches = [e for e in record.steps if e.node_name == "fetch"]
    assert [e.status for e in fetches] == ["retried", "retried", "pass"]
    raised = [
        tf.failure_class
        for e in fetches[:2]
        for tf in e.inspection.tool_failures
        if tf.failure_type == "tool_error"
    ]
    assert raised == [F, F]
    assert not [f for f in record.findings if f.type == "recovered_after_transient"]


def test_real_loop_with_one_transient_error_is_noise() -> None:
    record = _run_loop(_loop_app(TimeoutError("read timed out"), fail_times=1))
    assert [e.status for e in record.steps if e.node_name == "fetch"] == ["retried", "pass"]
    assert not [f for f in record.findings if f.type == "recovered_after_transient"]
