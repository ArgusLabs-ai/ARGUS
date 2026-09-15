"""Write a LangSmith-shaped trace of the demo graph, with no network and no account.

The demo graph (`demo/fat_trace/demo_graph.py`) runs under LangChain's own
`LangChainTracer`, handed a stub client that records every run payload the
tracer would have sent to LangSmith. Payloads are merged per run id the way the
client sends them: a `None` value is never sent, so it never overwrites.

That rule matters for the silent node: `summarize` returns `{}`, and its
end-of-run update carries no `outputs` at all (docs/prd_abhishek.md, BUG-1).

    PYTHONPATH=src python scripts/make_langsmith_fixture.py
    PYTHONPATH=src python scripts/make_langsmith_fixture.py --tool
    PYTHONPATH=src python scripts/make_langsmith_fixture.py --drop

``--tool`` traces a second graph instead: its `fetch` node calls a tool that
returns an HTTP 500 body, swallows it and returns a normal-looking update.

``--drop`` traces a four-node graph: `lookup` writes `customer_id`, `enrich`
nulls it, and `respond` reads it two steps later. Grade it with
``--consumers`` naming `respond` as the reader, and `enrich` is blamed.

``--llm`` traces a graph whose two nodes each call a scripted chat model (no
provider, no key): `outline` finishes normally on 20 tokens, `write` is cut
off at its token limit (`finish_reason: "length"`) on 40.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_core.tracers.langchain import LangChainTracer
from langgraph.graph import END, START, StateGraph

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "demo" / "fat_trace"))

from demo_graph import build_app  # noqa: E402

FIELDS = (
    "id",
    "trace_id",
    "parent_run_id",
    "name",
    "run_type",
    "inputs",
    "outputs",
    "error",
    "extra",
    "start_time",
    "end_time",
    "dotted_order",
    "tags",
)


class StubClient:
    """Stands in for `langsmith.Client`: records payloads, sends nothing."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}

    def _merge(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("id") or payload.get("run_id"))
        # Every field present, null until sent — the shape of a LangSmith export.
        row = self.runs.setdefault(run_id, {key: None for key in FIELDS} | {"id": run_id})
        for key in FIELDS:
            if key != "id" and payload.get(key) is not None:
                row[key] = payload[key]

    def create_run(self, **payload: Any) -> None:
        self._merge(payload)

    def update_run(self, **payload: Any) -> None:
        self._merge(payload)

    def flush(self) -> None:
        pass


class ToolState(TypedDict, total=False):
    query: str
    docs: list[str]
    answer: str


@tool
def fetch_docs(query: str) -> dict:
    """Look up documents for a query."""
    return {"status": 500, "body": "upstream down"}


def build_tool_app():
    def fetch(state: ToolState, config: RunnableConfig) -> dict:
        # The config carries the tracer to the tool on every Python version.
        fetch_docs.invoke({"query": state["query"]}, config=config)
        return {"docs": ["cached result"]}  # ← the 500 is swallowed

    def answer(state: ToolState) -> dict:
        return {"answer": f"Based on: {state['docs'][0]}"}

    graph = StateGraph(ToolState)
    graph.add_node("fetch", fetch)
    graph.add_node("answer", answer)
    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


class DropState(TypedDict, total=False):
    query: str
    customer_id: str | None
    profile: str
    draft: str
    reply: str


def build_drop_app():
    def lookup(state: DropState) -> dict:
        return {"customer_id": "c-42"}

    def enrich(state: DropState) -> dict:
        return {"profile": "gold tier", "customer_id": None}  # ← drops the id

    def draft(state: DropState) -> dict:
        return {"draft": f"Thanks for asking about: {state['query']}"}

    def respond(state: DropState) -> dict:
        return {"reply": f"{state['draft']} (customer {state.get('customer_id')})"}

    graph = StateGraph(DropState)
    for fn in (lookup, enrich, draft, respond):
        graph.add_node(fn.__name__, fn)
    graph.add_edge(START, "lookup")
    graph.add_edge("lookup", "enrich")
    graph.add_edge("enrich", "draft")
    graph.add_edge("draft", "respond")
    graph.add_edge("respond", END)
    return graph.compile()


class ScriptedChatModel(BaseChatModel):
    """Answers with fixed text, usage and finish reason: no network, no key."""

    text: str
    output_tokens: int
    finish_reason: str

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = AIMessage(
            content=self.text,
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": self.output_tokens,
                "total_tokens": 10 + self.output_tokens,
            },
            response_metadata={"model_name": "scripted", "finish_reason": self.finish_reason},
        )
        info = {"finish_reason": self.finish_reason}
        return ChatResult(
            generations=[ChatGeneration(message=message, generation_info=info)],
            llm_output={"model_name": "scripted"},
        )


class LLMState(TypedDict, total=False):
    query: str
    notes: str
    reply: str


def build_llm_app():
    short = ScriptedChatModel(text="three points", output_tokens=10, finish_reason="stop")
    cut = ScriptedChatModel(text="The first point is", output_tokens=30, finish_reason="length")

    def outline(state: LLMState, config: RunnableConfig) -> dict:
        return {"notes": short.invoke(state["query"], config=config).content}

    def write(state: LLMState, config: RunnableConfig) -> dict:
        return {"reply": cut.invoke(state["notes"], config=config).content}

    graph = StateGraph(LLMState)
    graph.add_node("outline", outline)
    graph.add_node("write", write)
    graph.add_edge(START, "outline")
    graph.add_edge("outline", "write")
    graph.add_edge("write", END)
    return graph.compile()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--tool", action="store_true", help="trace the tool graph instead")
    which.add_argument("--drop", action="store_true", help="trace the drop graph instead")
    which.add_argument("--llm", action="store_true", help="trace the LLM graph instead")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    if args.tool:
        name, app = "tool_graph.jsonl", build_tool_app()
    elif args.drop:
        name, app = "drop_graph.jsonl", build_drop_app()
    elif args.llm:
        name, app = "llm_graph.jsonl", build_llm_app()
    else:
        name, app = "demo_graph.jsonl", build_app()
    out = Path(args.out or REPO / "tests" / "fixtures" / "langsmith" / name)

    client = StubClient()
    tracer = LangChainTracer(client=client, project_name="argus-fixture")
    app.invoke({"query": "how do agents fail silently?"}, config={"callbacks": [tracer]})
    tracer.wait_for_futures()

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for row in client.runs.values():
            # The host's runtime details (OS, library versions) are not trace
            # content and have no place in a committed fixture.
            (row.get("extra") or {}).pop("runtime", None)
            fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")
    print(f"wrote {len(client.runs)} runs to {out}")


if __name__ == "__main__":
    main()
