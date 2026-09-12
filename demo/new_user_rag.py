"""What a new user would write: a five-node LangGraph RAG, then ARGUS.

ingest → retrieve → draft → cite → publish

draft does the work and returns {}. The graph still publishes. ARGUS should
fail the run on draft, not on publish.

    PYTHONPATH=src python demo/new_user_rag.py
    argus check last
    argus ui
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from argus import ArgusRecorder


class State(TypedDict, total=False):
    query: str
    sources: list[str]
    draft: str
    citations: list[str]
    report: str


def ingest(state: State) -> dict:
    return {"query": state["query"].strip()}


def retrieve(state: State) -> dict:
    return {"sources": [f"notes on {state['query']}", "second source"]}


def draft(state: State) -> dict:
    _ = " ".join(state.get("sources") or [])
    return {}  # silent no-op — the bug


def cite(state: State) -> dict:
    return {"citations": ["src-1", "src-2"]}


def publish(state: State) -> dict:
    return {"report": f"Published: {state.get('draft', '(nothing)')}"}


def build_app():
    g = StateGraph(State)
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


if __name__ == "__main__":
    app = ArgusRecorder(consumers={"sources": ["draft"]}).attach(build_app())
    result = app.invoke({"query": "How do agents fail silently?"})
    print("graph returned:", result)
    print("now:  argus check last")
