"""S-3: `argus ingest langsmith` grades an exported run with no app."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus.cli.main import app
from argus.ingest.langsmith import node_runs

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "langsmith" / "demo_graph.jsonl"
TOOL_FIXTURE = REPO / "tests" / "fixtures" / "langsmith" / "tool_graph.jsonl"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # finish() writes ARGUS_RUN_ID straight into os.environ; setenv first so
    # monkeypatch puts the original back afterwards.
    monkeypatch.setenv("ARGUS_RUN_ID", "")
    monkeypatch.delenv("ARGUS_EMBEDDINGS", raising=False)
    monkeypatch.setattr("argus.cloud.is_logged_in", lambda: False)


def test_the_silent_node_is_blamed_from_the_file_alone():
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", "langsmith", str(FIXTURE)])
    assert ingested.exit_code == 0, ingested.output

    checked = runner.invoke(app, ["check", "last", "--format", "json"])
    assert checked.exit_code == 1, checked.output
    assert json.loads(checked.output)["first_failure_step"] == "summarize"


def test_a_swallowed_tool_500_fails_the_node_that_called_the_tool():
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", "langsmith", str(TOOL_FIXTURE)])
    assert ingested.exit_code == 0, ingested.output

    checked = runner.invoke(app, ["check", "last", "--format", "json"])
    assert checked.exit_code == 1, checked.output
    payload = json.loads(checked.output)
    assert payload["first_failure_step"] == "fetch"
    critical = [
        f
        for f in payload["findings"]
        if f["node"] == "fetch"
        and f["severity"] == "critical"
        and "fetch_docs" in f["reason"]
        and "500" in f["reason"]
    ]
    assert critical, payload["findings"]


def test_logged_in_refuses_to_save_without_allow_cloud(monkeypatch):
    monkeypatch.setattr("argus.cloud.is_logged_in", lambda: True)
    result = CliRunner().invoke(app, ["ingest", "langsmith", str(FIXTURE)])
    assert result.exit_code == 2
    assert "--allow-cloud" in result.output
    assert list(Path(".argus/runs").iterdir()) == []


def test_a_subgraph_parent_row_is_not_a_step():
    def run(run_id, parent, node, step):
        return {
            "id": run_id,
            "parent_run_id": parent,
            "run_type": "chain",
            "tags": [f"graph:step:{step}"],
            "extra": {"metadata": {"langgraph_node": node, "langgraph_step": step}},
        }

    runs = [
        {"id": "root", "parent_run_id": None, "run_type": "chain", "tags": []},
        run("parent", "root", "research", 1),
        {"id": "inner-seq", "parent_run_id": "parent", "run_type": "chain", "tags": []},
        run("child", "inner-seq", "search", 1),
    ]
    assert [r["id"] for r in node_runs(runs)] == ["child"]


def test_the_ingest_module_imports_without_langgraph_or_langchain():
    blocker = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('langgraph', 'langchain_core', 'langchain'):\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, B())\n"
        "import argus.ingest.langsmith\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    subprocess.run([sys.executable, "-c", blocker], check=True, env=env)
