"""Write a LangSmith-shaped trace of the demo graph, with no network and no account.

The demo graph (`demo/fat_trace/demo_graph.py`) runs under LangChain's own
`LangChainTracer`, handed a stub client that records every run payload the
tracer would have sent to LangSmith. Payloads are merged per run id the way the
client sends them: a `None` value is never sent, so it never overwrites.

That rule matters for the silent node: `summarize` returns `{}`, and its
end-of-run update carries no `outputs` at all (docs/prd_abhishek.md, BUG-1).

    PYTHONPATH=src python scripts/make_langsmith_fixture.py
    PYTHONPATH=src python scripts/make_langsmith_fixture.py --tool
    PYTHONPATH=src python scripts/make_langsmith_fixture.py --crash

``--tool`` traces a second graph instead: its `fetch` node calls a tool that
returns an HTTP 500 body, swallows it and returns a normal-looking update.

``--crash`` traces a graph that raises: `lookup` writes `{"policy": {}}`,
`audit` runs in between and `price` reads `state["policy"]["number"]`.
"""

from __future__ import annotations

import argparse
import json
import sys
import sysconfig
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_core.tracers.langchain import LangChainTracer
from langgraph.graph import END, START, StateGraph

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "demo" / "fat_trace"))

from demo_graph import build_app  # noqa: E402

SITE_PACKAGES = str(Path(sysconfig.get_paths()["purelib"]))

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


class CrashState(TypedDict, total=False):
    query: str
    policy: dict
    notes: str
    price: float


def build_crash_app():
    def lookup(state: CrashState) -> dict:
        return {"policy": {}}  # ← cache miss: the container is written empty

    def audit(state: CrashState) -> dict:
        # A bystander: runs last before the crash and writes a new field.
        return {"notes": "audited"}

    def price(state: CrashState) -> dict:
        return {"price": state["policy"]["number"] * 1.0}

    graph = StateGraph(CrashState)
    graph.add_node("lookup", lookup)
    graph.add_node("audit", audit)
    graph.add_node("price", price)
    graph.add_edge(START, "lookup")
    graph.add_edge("lookup", "audit")
    graph.add_edge("audit", "price")
    graph.add_edge("price", END)
    return graph.compile()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tool", action="store_true", help="trace the tool graph instead")
    parser.add_argument("--crash", action="store_true", help="trace the crash graph instead")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    if args.crash:
        name, app = "crash_graph.jsonl", build_crash_app()
    elif args.tool:
        name, app = "tool_graph.jsonl", build_tool_app()
    else:
        name, app = "demo_graph.jsonl", build_app()
    out = Path(args.out or REPO / "tests" / "fixtures" / "langsmith" / name)

    client = StubClient()
    tracer = LangChainTracer(client=client, project_name="argus-fixture")
    try:
        app.invoke({"query": "how do agents fail silently?"}, config={"callbacks": [tracer]})
    except KeyError:
        if not args.crash:
            raise  # only the crash graph is meant to raise
    tracer.wait_for_futures()

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for row in client.runs.values():
            # The host's runtime details (OS, library versions) are not trace
            # content and have no place in a committed fixture.
            (row.get("extra") or {}).pop("runtime", None)
            if row.get("error"):
                # Tracebacks name this machine's paths; keep the text, not the host.
                for prefix, label in ((SITE_PACKAGES, "<site-packages>"), (str(REPO), "<repo>")):
                    row["error"] = row["error"].replace(prefix, label)
            fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")
    print(f"wrote {len(client.runs)} runs to {out}")


if __name__ == "__main__":
    main()
