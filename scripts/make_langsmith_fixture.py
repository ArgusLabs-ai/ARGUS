"""Write a LangSmith-shaped trace of the demo graph, with no network and no account.

The demo graph (`demo/fat_trace/demo_graph.py`) runs under LangChain's own
`LangChainTracer`, handed a stub client that records every run payload the
tracer would have sent to LangSmith. Payloads are merged per run id the way the
client sends them: a `None` value is never sent, so it never overwrites.

That rule matters for the silent node: `summarize` returns `{}`, and its
end-of-run update carries no `outputs` at all (docs/prd_abhishek.md, BUG-1).

    PYTHONPATH=src python scripts/make_langsmith_fixture.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from langchain_core.tracers.langchain import LangChainTracer

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", default=str(REPO / "tests" / "fixtures" / "langsmith" / "demo_graph.jsonl")
    )
    args = parser.parse_args()

    client = StubClient()
    tracer = LangChainTracer(client=client, project_name="argus-fixture")
    build_app().invoke({"query": "how do agents fail silently?"}, config={"callbacks": [tracer]})
    tracer.wait_for_futures()

    out = Path(args.out)
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
