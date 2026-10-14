"""Deterministic retryability taxonomy for tool failures (PRD-02).

Answers "would running it again help?" for every ``ToolFailure``, with zero LLM
calls: a table lookup, one HTTP-status branch and a few word-bounded phrase
checks. The class is additive; it never changes a ``failure_type`` or a severity.

Classes:

* ``rate_limited`` -- the service said "slow down" (HTTP 429, or the words
  "rate limit" / "too many requests"). Re-queue with backoff.
* ``transient`` -- the call failed for a reason that usually passes: an HTTP
  408/425/5xx status, or text that names a timeout, a connection reset, an
  overloaded or temporarily unavailable service.
* ``fatal`` -- anything else. A 4xx, an empty result, a malformed payload, a
  quality signal, a tool that raised for a reason that is not obviously
  transient: the same input fails the same way, so fix the call.

``fatal`` is the safe default: transient needs *explicit* evidence. A payload
error with no HTTP status and no transient wording (``{"error": "Bad request"}``,
``success: False``) is fatal, so a real bug is never filed under "retry once".
Deliberate calls: "connection refused" and DNS failures are fatal (usually a
wrong host, port or config, not a blip); "quota exceeded" is fatal (a billing
limit, not a rate); a bare "timeout" is transient unless the text also reads as
input validation ("invalid timeout", "parameter ...").

Never raises: an unknown or malformed ``failure_type`` is fatal (logged at
debug), so an old or hand-edited run record always loads.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

CLASS_FATAL = "fatal"
CLASS_TRANSIENT = "transient"
CLASS_RATE_LIMITED = "rate_limited"
FAILURE_CLASSES = frozenset({CLASS_FATAL, CLASS_TRANSIENT, CLASS_RATE_LIMITED})

# failure_type -> class when the type alone decides. ``rate_limit`` is the only
# non-fatal one; the rest are every other type emitted today (one place to read
# "what does each type mean for a retry").
_FIXED: dict[str, str] = {"rate_limit": CLASS_RATE_LIMITED}
_FATAL_TYPES = frozenset(
    """empty_result partial_failure json_in_string semantic_degradation
    placeholder_detected structural_anomaly truncated_output confidence_mismatch
    retrieval_quality_low shallow_context shallow_output
    information_compression_anomaly selective_attention_reduction input_echo
    semantic_contradiction context_size_anomaly empty_output
    subgraph_no_contribution timeout_adjacent suspiciously_fast
    latency_quality_mismatch missing_field_guess review_verified review_confirmed
    stuck_loop missing_output_key unknown_state_key type_drift sentinel_value
    unrendered_template degenerate_repetition ungrounded_number
    near_miss_identifier unperformed_action status_overstated
    unparseable_model_json incomplete_result unfollowed_pagination""".split()
)
# Types whose class depends on the call's own evidence (HTTP status / text).
_EVIDENCE_TYPES = frozenset({"error_response", "error_in_data", "tool_error"})

_logged_unknown: set[str] = set()

# Retry-worthy statuses. 429 is handled apart (rate_limited); 501/505/511 are
# server-side but deterministic ("not implemented", "version unsupported",
# "network auth required"), so they stay fatal like every 4xx.
_TRANSIENT_STATUSES = frozenset({408, 425})
_FATAL_5XX = frozenset({501, 505, 511})

# Text longer than this is read head + tail only: a traceback ends with the
# exception line, a log blob starts with the cause, and the scan stays bounded.
_WINDOW = 2000

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# A number followed by a unit or a count noun is a quantity, not a status:
# "500 MB file limit exceeded", "500 items required", "(500) rows". Bare seconds
# are not one after a 429: "429 seconds" stays a rate limit, "503 seconds" does not.
_QUANTITY = (
    r"(?!\)?\s*(?:[kmgt]i?b|bytes?|chars?|characters?|items?|rows?|records?|lines?|files?"
    r"|tokens?|words?|results?|entries|pages?|ms|mins?|minutes?|hours?|days?)"
    r"(?![a-z0-9]))"
    r"(?!(?<!429)\)?\s*(?:s|secs?|seconds?)(?![a-z0-9]))"
)
# After a call verb a plural noun ("got 500 new messages") or a bare "limit" / "id" is a
# count: "got 500 errors" and "failed (429) records skipped" are not statuses, while
# "got a 429 from the API", "hit a 429 rate limit" and "got 429 after retries" are.
# "429 limit(s)" is a rate limit, so only a 5xx "500 limit" is a count. The one skipped word
# is an adjective, not a preposition or connective ("got 429 during retries"); words that
# merely end in s ("got 503 news", "always", "series") are not plurals.
_COUNTED = (
    r"(?:(?<=429)(?=\)?\s+limits?(?![a-z]))"
    r"|(?!\)?\s+(?:limit|ids?"
    r"|(?:(?!(?:and|or|but|then|after|before|from|with|while|when|on|in|at|for|to|of|by|as"
    r"|until|since|because|during|despite|including|via|per|under|across|over|into|about"
    r"|even|using|upon|through(?:out)?|between|among|against|within|without|towards?|onto"
    r"|along|around|beyond|behind|below|above|near|besides|that|which|where|though"
    r"|although|unless|whereas)(?![a-z]))[a-z]+\s+)?"
    r"(?!(?:news|lens|series|species|always|sometimes|perhaps|afterwards|towards|besides"
    r"|whereas|means|does|says|seems)(?![a-z]))[a-z]{2,}[^\Wsui\d_]s)(?![a-z])))"
)
# A status counts only next to a word that makes it an HTTP code ("HTTP 503",
# "status code: 429", "Client error '404 Not Found'", {"code": 503}, "got a 429",
# "HTTPError: 429", "ResourceExhausted: 429") or as the first token of the
# message ("401 Unauthorized: ..."). A bare number is not a status: "port 4290",
# "customer 404", "order 1429" must stay unclassified.
_STATUS_CONTEXT_RE = re.compile(
    r"(?<![a-z])(?:http(?:status)?error|http(?:/\d(?:\.\d)?)?|status(?:[ _]?code)?"
    r"|response(?:[ _]?code)?|resource[ _]?exhausted|code|error|responded with|failed with"
    r"|(?P<call>(?:returned|got|received)(?:\s+an?)?|hit\s+an?|failed(?=\s*[:(])))(?![a-z])"
    r"[\s:=#'\"(\[-]{0,3}(?P<n>[45]\d\d)(?![0-9a-z])" + _QUANTITY + r"(?(call)" + _COUNTED + r")",
    re.IGNORECASE,
)
_STATUS_LEADING_RE = re.compile(
    r"^[\s'\"(\[]*(?P<n>[45]\d\d)(?![0-9a-z.])" + _QUANTITY + r"(?=[\s:,.\-)\]'\"]*(?:[a-z]|$))",
    re.IGNORECASE,
)

# Phrases, matched on lower-cased, camel-split text with letter-only
# boundaries (so "accurate limit" and "port 4290" never match, while
# "rate_limit_exceeded" and "RateLimitError" do).
_RATE_LIMIT_RE = re.compile(
    r"(?<![a-z])(?:rate[ _-]?limit(?:s|ed|ing)?|too[ _-]?many[ _-]?requests"
    r"|throttl(?:e|ed|es|ing))(?![a-z])"
)
_TRANSIENT_RE = re.compile(
    r"(?<![a-z])(?:time[_-]?outs?|timed[ _-]?out|etimedout|deadline[ _]exceeded"
    r"|econnreset|connection[ _-](?:reset|aborted)|reset by peer|broken pipe"
    r"|server[ _]disconnected|remote(?:ly)?[ _]?(?:end[ _]closed|disconnected)"
    r"|(?:temporarily|currently)[ _]unavailable|service[ _]unavailable|bad[ _]gateway"
    r"|internal[ _]server[ _]error|overloaded|retry[ _-]after"
    r"|try[ _]again[ _](?:later|in|shortly))(?![a-z])"
    r"|^\W*unavailable\W*$"
)
# "timeout" as a parameter name or an input complaint is not a timeout.
_VALIDATION_RE = re.compile(
    r"(?<![a-z])(?:invalid|must be|should be|not allowed|unsupported|malformed"
    r"|parameters?|arguments?)(?![a-z])"
)


def valid_failure_class(value: Any) -> Optional[str]:
    """``value`` when it is one of the three classes, else ``None``."""
    return value if isinstance(value, str) and value in FAILURE_CLASSES else None


def coerce_status(value: Any) -> Optional[int]:
    """An HTTP status from an int or an int-like string (``"503"``); else ``None``.

    Bools and anything outside 100-599 are not statuses.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        value = int(value) if value.isdigit() and len(value) <= 3 else None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _window(text: str) -> str:
    return text if len(text) <= 2 * _WINDOW else f"{text[:_WINDOW]} {text[-_WINDOW:]}"


def status_from_text(text: Any) -> Optional[int]:
    """The 4xx/5xx status a message states (``"HTTP 503"``, ``"401 Unauthorized"``)."""
    if not isinstance(text, str):
        return None
    text = _window(text)
    m = _STATUS_CONTEXT_RE.search(text) or _STATUS_LEADING_RE.search(text)
    return int(m.group("n")) if m else None


def _class_for_status(status: int) -> Optional[str]:
    if status == 429:
        return CLASS_RATE_LIMITED
    if status in _TRANSIENT_STATUSES or (500 <= status <= 599 and status not in _FATAL_5XX):
        return CLASS_TRANSIENT
    if 400 <= status <= 599:
        return CLASS_FATAL
    return None  # 1xx-3xx: not an error status, says nothing either way


def _normalize(text: str) -> str:
    return _CAMEL_RE.sub(" ", _window(text)).lower()


def is_rate_limit_text(text: Any) -> bool:
    """Does this error text say the call was rate limited?

    HTTP 429 in context or the words "rate limit" / "too many requests" /
    "throttled". Not quota or billing ("quota exceeded"), not a bare "429" inside
    another number, not "too many request parameters".
    """
    if not isinstance(text, str):
        return False
    return status_from_text(text) == 429 or bool(_RATE_LIMIT_RE.search(_normalize(text)))


def _classify_evidence(status: Any, text: Any) -> str:
    code = coerce_status(status)
    if code is None:
        code = status_from_text(text)
    by_status = _class_for_status(code) if code is not None else None
    if by_status is not None:
        return by_status
    if isinstance(text, str) and text:
        norm = _normalize(text)
        if _RATE_LIMIT_RE.search(norm):
            return CLASS_RATE_LIMITED
        if _TRANSIENT_RE.search(norm) and not _VALIDATION_RE.search(norm):
            return CLASS_TRANSIENT
    return CLASS_FATAL


def classify_failure_class(failure_type: Any, status: Any = None, text: Any = None) -> str:
    """Return ``"fatal" | "transient" | "rate_limited"`` for one failure.

    ``status`` is the HTTP status of the failing call when known (an int or an
    int-like string); ``text`` is the error text (payload message or exception
    repr) for the types that read it: ``error_response``, ``error_in_data`` and
    ``tool_error``. With no usable status the text is searched for one, then for
    unambiguous transient wording; with neither the answer is ``fatal``.
    Pure and deterministic, never raises.
    """
    if not isinstance(failure_type, str):
        log.debug("non-string failure_type %r: defaulting to fatal", str(failure_type)[:80])
        return CLASS_FATAL
    if failure_type in _FIXED:
        return _FIXED[failure_type]
    if failure_type in _EVIDENCE_TYPES:
        return _classify_evidence(status, text)
    if (
        failure_type not in _FATAL_TYPES
        and failure_type not in _logged_unknown
        and len(_logged_unknown) < 64
    ):
        _logged_unknown.add(failure_type)
        log.debug("unclassified failure_type %r: defaulting to fatal", failure_type[:80])
    return CLASS_FATAL
