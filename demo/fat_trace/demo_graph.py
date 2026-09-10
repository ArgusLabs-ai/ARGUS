"""Spike 1 proof: a silent no-op fails the gate with no engine wrap.

`summarize` searches, throws the result away and returns `{}`. LangGraph merges
that into a state still full of `docs`, so the run looks healthy and `answer`
happily produces text. Nothing crashes. `argus check` still fails, and blames
`summarize` — not `answer`.

    python demo/fat_trace/demo_graph.py
    argus check            # exit 1
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from argus import ArgusRecorder


class ResearchState(TypedDict, total=False):
    query: str
    docs: list[str]
    summary: str
    answer: str


def search(state: ResearchState) -> dict:
    return {"docs": [f"result for {state['query']}", "another result"]}


def summarize(state: ResearchState) -> dict:
    _ = " ".join(state.get("docs", []))  # work done, result discarded
    return {}  # ← the silent no-op


def answer(state: ResearchState) -> dict:
    return {"answer": f"Based on the summary: {state.get('summary', '(nothing)')}"}


def build_app():
    graph = StateGraph(ResearchState)
    graph.add_node("search", search)
    graph.add_node("summarize", summarize)
    graph.add_node("answer", answer)
    graph.add_edge(START, "search")
    graph.add_edge("search", "summarize")
    graph.add_edge("summarize", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


def main() -> None:
    app = ArgusRecorder().attach(build_app())  # ← the whole user API
    result = app.invoke({"query": "how do agents fail silently?"})

    print("graph returned:", result)
    print("\nthe graph succeeded. now ask ARGUS:  argus check")


if __name__ == "__main__":
    main()
