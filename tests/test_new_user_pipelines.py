"""A new user's LangGraph research pipeline, watched the pivot way.

No API key. No network. The graph is a real five-node RAG (ingest → retrieve
→ draft → cite → publish) with a tool on retrieve. ARGUS is only:

    from argus import ArgusRecorder
    app = ArgusRecorder(consumers=...).attach(app)
    app.invoke(...)
    # then argus check

The old wrap used to score empty-on-purpose at ingest as a failure. That is
the five-node false-positive this file guards against.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from argus.check import evaluate_run
from argus.ledger import build_ledger
from argus.recorder import ArgusRecorder
from argus.storage import load_run

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

# Who still needs a field later — not "the next node".
CONSUMERS = {
    "sources": ["draft"],
    "draft": ["cite"],
    "citations": ["publish"],
}


class PipelineState(TypedDict, total=False):
    query: str
    sources: list[str]
    draft: str
    citations: list[str]
    report: str


@tool
def search_corpus(q: str) -> list[str]:
    """Look up source passages for a query."""
    return [f"passage about {q}", "another passage"]


def _no_patching(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("patch_graph was called — a new user is on ArgusRecorder")

    monkeypatch.setattr("argus.patcher.patch_graph", _boom)


def _pipeline(*, retrieve_docs, draft_text, citations):
    """Build the graph. Nodes return updates, the way LangGraph is actually written."""

    def ingest(state: PipelineState) -> dict:
        # Empty-on-purpose: sources / draft / citations fill later.
        return {"query": state["query"].strip().lower()}

    def retrieve(state: PipelineState) -> dict:
        return {"sources": retrieve_docs(state["query"])}

    def draft(state: PipelineState) -> dict:
        return {"draft": draft_text(state)}

    def cite(state: PipelineState) -> dict:
        return {"citations": citations(state)}

    def publish(state: PipelineState) -> dict:
        return {
            "report": (
                f"{state.get('draft', '')} "
                f"[{', '.join(state.get('citations') or [])}]"
            ).strip()
        }

    g = StateGraph(PipelineState)
    g.add_node("ingest", ingest)
    g.add_node("retrieve", retrieve)
    g.add_node("draft", draft)
    g.add_node("cite", cite)
    g.add_node("publish", publish)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "cite")
    g.add_edge("cite", "publish")
    g.add_edge("publish", END)
    return g.compile()


def _healthy():
    return _pipeline(
        retrieve_docs=lambda q: search_corpus.invoke({"q": q}),
        draft_text=lambda s: "Findings: " + "; ".join(s.get("sources") or []),
        citations=lambda s: [f"src-{i}" for i, _ in enumerate(s.get("sources") or [], 1)],
    )


def _run(monkeypatch, app, **recorder_kw):
    _no_patching(monkeypatch)
    recorder = ArgusRecorder(consumers=CONSUMERS, **recorder_kw)
    result = recorder.attach(app).invoke({"query": "How do agents fail silently?"})
    return result, load_run(recorder.session.run_id)


@pytest.mark.integration
def test_a_healthy_five_node_rag_is_clean(monkeypatch):
    """Ingest has no sources yet. That is not a silent failure."""
    result, record = _run(monkeypatch, _healthy())

    assert result["report"]
    assert evaluate_run(record).passed is True
    assert record.overall_status == "clean"
    assert record.findings == []

    rows = {r.node: r for r in build_ledger(record.steps, record.initial_state)}
    assert rows["ingest"].update == {"query": "how do agents fail silently?"}
    assert "sources" not in rows["ingest"].update
    assert rows["retrieve"].update["sources"]
    assert rows["retrieve"].tools[0]["name"] == "search_corpus"
    assert rows["draft"].state_after["sources"], "later nodes still see what retrieve wrote"


@pytest.mark.integration
def test_silent_draft_fails_on_draft_not_publish(monkeypatch):
    """draft returns {}. The graph still publishes. ARGUS blames draft."""

    def draft_empty(state: PipelineState) -> dict:
        _ = state.get("sources")
        return {}

    def ingest(state: PipelineState) -> dict:
        return {"query": state["query"].strip().lower()}

    def retrieve(state: PipelineState) -> dict:
        return {"sources": ["a passage"]}

    def cite(state: PipelineState) -> dict:
        return {"citations": ["src-1"]}

    def publish(state: PipelineState) -> dict:
        return {"report": f"Based on: {state.get('draft', '(nothing)')}"}

    g = StateGraph(PipelineState)
    g.add_node("ingest", ingest)
    g.add_node("retrieve", retrieve)
    g.add_node("draft", draft_empty)
    g.add_node("cite", cite)
    g.add_node("publish", publish)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "cite")
    g.add_edge("cite", "publish")
    g.add_edge("publish", END)

    result, record = _run(monkeypatch, g.compile())
    verdict = evaluate_run(record)

    assert result["report"] == "Based on: (nothing)"
    assert verdict.passed is False
    assert "draft" in verdict.failing_nodes
    assert "publish" not in verdict.failing_nodes
    assert "cite" not in verdict.failing_nodes
    empty = [f for f in record.findings if f.type == "empty_output"]
    assert empty and empty[0].node == "draft"


@pytest.mark.integration
def test_placeholder_draft_fails_the_gate_without_an_llm(monkeypatch):
    """Literal PLACEHOLDER in the draft — signature rule, no model."""
    app = _pipeline(
        retrieve_docs=lambda q: ["a passage"],
        draft_text=lambda s: "PLACEHOLDER",
        citations=lambda s: ["src-1"],
    )
    _result, record = _run(monkeypatch, app)
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert "draft" in verdict.failing_nodes
    kinds = {f.type for f in record.findings}
    assert "placeholder_detected" in kinds or any(
        f.type == "placeholder_outputs" for f in record.findings
    )


@pytest.mark.integration
def test_empty_retrieval_fails_on_retrieve(monkeypatch):
    app = _pipeline(
        retrieve_docs=lambda q: [],
        draft_text=lambda s: "nothing to draft",
        citations=lambda s: [],
    )
    _result, record = _run(monkeypatch, app)
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert "retrieve" in verdict.failing_nodes
    assert any(
        f.type == "empty_result" and f.node == "retrieve" for f in record.findings
    )


@pytest.mark.integration
def test_missing_sources_blames_retrieve_not_draft(monkeypatch):
    """draft reads sources; retrieve wrote the wrong key.

    The one-rule walk blames the first row whose running state lacks `sources`
    — ingest, which never had them. That is origin, not draft (the reader).
    """

    def ingest(state: PipelineState) -> dict:
        return {"query": state["query"].strip().lower()}

    def retrieve_no_sources(state: PipelineState) -> dict:
        return {"notes": "searched, wrote the wrong key"}

    def draft(state: PipelineState) -> dict:
        return {"draft": f"used {state.get('sources')}"}

    def cite(state: PipelineState) -> dict:
        return {"citations": ["src-1"]}

    def publish(state: PipelineState) -> dict:
        return {"report": state.get("draft", "")}

    g = StateGraph(PipelineState)
    for name, fn in (
        ("ingest", ingest),
        ("retrieve", retrieve_no_sources),
        ("draft", draft),
        ("cite", cite),
        ("publish", publish),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "cite")
    g.add_edge("cite", "publish")
    g.add_edge("publish", END)

    _result, record = _run(monkeypatch, g.compile())
    verdict = evaluate_run(record)

    assert verdict.passed is False
    assert "retrieve" in verdict.failing_nodes or "ingest" in verdict.failing_nodes
    assert "draft" not in verdict.failing_nodes
    assert "publish" not in verdict.failing_nodes
    miss = [f for f in record.findings if f.type == "missing_field" and f.field_path == "sources"]
    assert miss
    assert miss[0].node in {"ingest", "retrieve"}
    # origin is the first row whose running state lacks sources: ingest never
    # had them, so ingest is the honest origin under the one-rule walk.
    assert miss[0].node == "ingest"


@pytest.mark.integration
def test_new_user_cli_argus_check_fails_the_silent_graph(monkeypatch):
    """The whole new-user loop: attach, invoke, `argus check last`."""
    from argus.cli.main import app as cli

    _no_patching(monkeypatch)

    def ingest(state: PipelineState) -> dict:
        return {"query": state["query"]}

    def retrieve(state: PipelineState) -> dict:
        return {"sources": ["p"]}

    def draft(state: PipelineState) -> dict:
        return {}

    def cite(state: PipelineState) -> dict:
        return {"citations": ["s"]}

    def publish(state: PipelineState) -> dict:
        return {"report": "ok"}

    g = StateGraph(PipelineState)
    for name, fn in (
        ("ingest", ingest),
        ("retrieve", retrieve),
        ("draft", draft),
        ("cite", cite),
        ("publish", publish),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "cite")
    g.add_edge("cite", "publish")
    g.add_edge("publish", END)

    ArgusRecorder().attach(g.compile()).invoke({"query": "q"})

    result = CliRunner().invoke(cli, ["check", "last"])
    assert result.exit_code == 1, result.output
    output = result.output.lower()
    assert "silent_failure" in output or "empty_output" in output or "fail" in output
