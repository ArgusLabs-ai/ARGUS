"""E5 / #132: bracketed placeholders inside prose are a soft flag.

PH-014 only matches when the whole value is the placeholder, so
``Hi Dana, loved your recent post about [TOPIC]`` shipped clean. PH-015
matches 1–4 capitalised words in square brackets, ``[INSERT …]``, and
``{{var}}`` / ``{var}`` anywhere in the string. It is a warning: citations
``[1]``, markdown links, and ``[Draft]`` must not match, and a warning does
not fail ``argus check``.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.recorder import ArgusRecorder
from argus.registry import scan_value
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402

pytestmark = pytest.mark.unit

_FLAGGED = [
    "Hi Dana, loved your recent post about [TOPIC]. Best, [Your Name]",
    "Dear [Claimant Name], your claim [CLAIM ID] has been processed.",
    "Please [INSERT ADDRESS] before sending.",
    "Hello {{customer_name}}, your id is {order_id}.",
]
_CLEAN = [
    "see [1] and [2]",
    "[docs](https://example.com)",
    "Status: [Draft]",
    "ACME's Q3 revenue grew 12% year over year.",
]


def _ph015(text: str) -> list:
    return [m for m in scan_value(text) if m.sig_id == "PH-015"]


@pytest.mark.parametrize("text", _FLAGGED)
def test_placeholders_inside_prose_are_flagged(text):
    matches = _ph015(text)
    assert matches, text
    assert all(m.severity == "warning" for m in matches)


@pytest.mark.parametrize("text", _CLEAN)
def test_citations_links_and_draft_are_not_flagged(text):
    assert _ph015(text) == [], text


def test_a_placeholder_in_an_email_body_is_a_warning_not_a_ci_fail():
    class State(TypedDict, total=False):
        email: dict
        sent: bool

    def draft(state: State) -> dict:
        return {
            "email": {
                "subject": "hello",
                "body": "Hi Dana, loved your recent post about [TOPIC]. Best, [Your Name]",
            }
        }

    def send(state: State) -> dict:
        return {"sent": True}

    g = StateGraph(State)
    g.add_node("draft", draft)
    g.add_node("send", send)
    g.add_edge(START, "draft")
    g.add_edge("draft", "send")
    g.add_edge("send", END)

    recorder = ArgusRecorder(semantic_judge=False)
    recorder.attach(g.compile()).invoke({})
    record = load_run(recorder.session.run_id)
    verdict = evaluate_run(record)
    assert verdict.passed, verdict
    flagged = [f for f in record.findings if "PH-015" in (f.reason or "")]
    assert flagged, record.findings
    assert all(f.severity == "warning" for f in flagged)
