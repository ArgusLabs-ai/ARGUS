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
from argus.ingest.langsmith import load_runs, node_runs, tool_calls_by_step
from argus.storage import list_runs, load_run

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "langsmith" / "demo_graph.jsonl"
TOOL_FIXTURE = REPO / "tests" / "fixtures" / "langsmith" / "tool_graph.jsonl"
DROP_FIXTURE = REPO / "tests" / "fixtures" / "langsmith" / "drop_graph.jsonl"


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


def test_a_tool_result_is_unwrapped_to_what_the_recorder_hears():
    # LangSmith stores `outputs={"output": <result>}`; on_tool_end hears the bare result.
    runs = load_runs(TOOL_FIXTURE)
    steps = node_runs(runs)
    fetch = next(r for r in steps if r["extra"]["metadata"]["langgraph_node"] == "fetch")
    [call] = tool_calls_by_step(runs, steps)[str(fetch["id"])]
    assert call["name"] == "fetch_docs"
    assert call["output"] == {"status": 500, "body": "upstream down"}
    assert call["error"] is None


def test_a_tool_result_with_no_output_key_keeps_its_payload():
    """An export that does not wrap the result must not lose it.

    `outputs["output"]` is LangSmith's shape, not a guarantee. Reading that key
    blind hands the graders `None` for anything else — and a dropped payload is
    a tool failure nobody sees, which is the whole point of reading tools.
    """
    import copy

    runs = copy.deepcopy(load_runs(TOOL_FIXTURE))
    for run in runs:
        if run.get("run_type") == "tool":
            run["outputs"] = {"status": 500, "body": "upstream down"}
    steps = node_runs(runs)
    fetch = next(r for r in steps if r["extra"]["metadata"]["langgraph_node"] == "fetch")
    [call] = tool_calls_by_step(runs, steps)[str(fetch["id"])]
    assert call["output"] == {"status": 500, "body": "upstream down"}


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


def _new_run_after(runner_call):
    before = {r["run_id"] for r in list_runs()}
    runner_call()
    [run_id] = {r["run_id"] for r in list_runs()} - before
    return load_run(run_id)


def test_edges_from_the_demo_graph_match_the_recorder(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO))
    from demo.fat_trace.demo_graph import build_app

    from argus import ArgusRecorder

    runner = CliRunner()
    exported = runner.invoke(
        app, ["edges", "demo.fat_trace.demo_graph:build_app", "--out", "edges.json"]
    )
    assert exported.exit_code == 0, exported.output

    live = _new_run_after(
        lambda: ArgusRecorder(semantic_judge=False).attach(build_app()).invoke({"query": "q"})
    )

    def ingest():
        result = runner.invoke(app, ["ingest", "langsmith", str(FIXTURE), "--edges", "edges.json"])
        assert result.exit_code == 0, result.output

    ingested = _new_run_after(ingest)
    assert ingested.graph_edge_map == live.graph_edge_map

    checked = runner.invoke(app, ["check", "last", "--format", "json"])
    assert checked.exit_code == 1, checked.output
    assert json.loads(checked.output)["first_failure_step"] == "summarize"


def test_the_edges_file_replaces_the_step_order_guess():
    # Step order can only ever say search -> summarize; this map says more.
    edges = {
        "edge_map": {"search": ["summarize", "answer"], "summarize": ["answer"]},
        "conditional_sources": ["search"],
        "node_names": ["search", "summarize", "answer"],
        "subgraph_parents": [],
    }
    Path("edges.json").write_text(json.dumps(edges))
    runner = CliRunner()

    def ingest():
        result = runner.invoke(app, ["ingest", "langsmith", str(FIXTURE), "--edges", "edges.json"])
        assert result.exit_code == 0, result.output

    assert _new_run_after(ingest).graph_edge_map == edges["edge_map"]


def test_the_ingest_actually_uses_the_files_subgraph_parents():
    """The wiring, not just `node_runs`.

    The sibling unit test calls `node_runs(runs, parents)` directly, so it
    stays green even if `ingest_langsmith` stops passing the file's
    `subgraph_parents` at all. Naming a node the trace really contains is the
    observable check: declared a subgraph parent, it must not become a step —
    and since the named node here is the silent one, the run goes clean, which
    step-order dropping could never produce.
    """
    edges = {
        "edge_map": {"search": ["answer"]},
        "conditional_sources": [],
        "node_names": ["search", "answer"],
        "subgraph_parents": ["summarize"],
    }
    Path("edges.json").write_text(json.dumps(edges))
    runner = CliRunner()

    def ingest():
        result = runner.invoke(app, ["ingest", "langsmith", str(FIXTURE), "--edges", "edges.json"])
        assert result.exit_code == 0, result.output

    record = _new_run_after(ingest)
    assert [s.node_name for s in record.steps] == ["search", "answer"], (
        "the node the edges file calls a subgraph parent must not be a step"
    )


def test_an_edges_file_for_another_graph_refuses_instead_of_grading_clean():
    """A stale or wrong edges file must not quietly turn a failure into a pass.

    The map is well-formed but describes a different graph, so `summarize` has
    no successors in it and `empty_output` — the whole reason this fixture
    fails — cannot fire. Ingesting it graded the run **clean**, exit 0: the
    "no findings, so it passed" outcome the brief bans, reached by pointing at
    the wrong file. Easy to hit for real after a graph is refactored and
    `edges.json` is not re-exported.
    """
    edges = {
        "edge_map": {"alpha": ["beta"], "beta": ["gamma"]},
        "conditional_sources": [],
        "node_names": ["alpha", "beta", "gamma"],
        "subgraph_parents": [],
    }
    Path("edges.json").write_text(json.dumps(edges))
    result = CliRunner().invoke(
        app, ["ingest", "langsmith", str(FIXTURE), "--edges", "edges.json"]
    )

    assert result.exit_code == 2, result.output
    assert "does not describe" in result.output
    assert "argus edges" in result.output
    assert not list_runs(), "a run we refused to grade must not be saved"


def test_an_unreadable_edges_file_saves_nothing():
    Path("edges.json").write_text(json.dumps({"edge_map": {}}))
    result = CliRunner().invoke(
        app, ["ingest", "langsmith", str(FIXTURE), "--edges", "edges.json"]
    )
    assert result.exit_code == 2
    assert "argus edges" in result.output
    assert list(Path(".argus/runs").iterdir()) == []


def test_edges_name_the_subgraph_parents_instead_of_nesting():
    def run(run_id, parent, node):
        return {
            "id": run_id,
            "parent_run_id": parent,
            "run_type": "chain",
            "tags": ["graph:step:1"],
            "extra": {"metadata": {"langgraph_node": node, "langgraph_step": 1}},
        }

    runs = [
        {"id": "root", "parent_run_id": None, "run_type": "chain", "tags": []},
        run("parent", "root", "research"),
        run("child", "parent", "search"),
        run("sibling", "root", "report"),
    ]
    # The nested parent stays (the file says it is no subgraph); the named one goes.
    assert sorted(r["id"] for r in node_runs(runs, {"report"})) == ["child", "parent"]


def _skinny_copy(change):
    """The demo fixture with ``change(row)`` applied to every row, written to cwd."""
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    for row in rows:
        change(row)
    path = Path("skinny.jsonl")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_skinny_trace_with_outputs_hidden_saves_nothing():
    # hide_outputs=True gives every run, root included, outputs == {}.
    path = _skinny_copy(lambda row: row.update(outputs={}))
    result = CliRunner().invoke(app, ["ingest", "langsmith", str(path)])
    assert result.exit_code == 2, result.output
    assert "root run has no outputs" in result.output
    assert list(Path(".argus/runs").iterdir()) == []


def test_skinny_node_run_without_inputs_saves_nothing():
    def drop_search_inputs(row):
        if row.get("name") == "search":
            row.pop("inputs")

    path = _skinny_copy(drop_search_inputs)
    result = CliRunner().invoke(app, ["ingest", "langsmith", str(path)])
    assert result.exit_code == 2, result.output
    assert "search" in result.output
    assert list(Path(".argus/runs").iterdir()) == []


def test_skinny_trace_with_no_node_runs_saves_nothing():
    path = _skinny_copy(lambda row: None)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text(json.dumps(next(r for r in rows if r["parent_run_id"] is None)) + "\n")
    result = CliRunner().invoke(app, ["ingest", "langsmith", str(path)])
    assert result.exit_code == 2, result.output
    assert list(Path(".argus/runs").iterdir()) == []


def test_not_skinny_when_only_one_node_has_empty_outputs():
    # The fixture's summarize already arrives as {}; root outputs prove nothing was hidden.
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    assert [r["name"] for r in rows if r.get("outputs") == {}] == ["summarize"]
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", "langsmith", str(FIXTURE)])
    assert ingested.exit_code == 0, ingested.output
    checked = runner.invoke(app, ["check", "last", "--format", "json"])
    assert checked.exit_code == 1, checked.output
    assert json.loads(checked.output)["first_failure_step"] == "summarize"


def test_consumers_blame_the_step_that_dropped_the_field():
    """`enrich` nulls `customer_id`; `respond` reads it two steps later.

    Adjacent matching would blame `draft` (the step before the reader) or
    `respond` (where the gap shows). The declared reader anchors the walk back.
    """
    Path("consumers.json").write_text(json.dumps({"customer_id": ["respond"]}))
    runner = CliRunner()
    ingested = runner.invoke(
        app, ["ingest", "langsmith", str(DROP_FIXTURE), "--consumers", "consumers.json"]
    )
    assert ingested.exit_code == 0, ingested.output

    checked = runner.invoke(app, ["check", "last", "--format", "json"])
    assert checked.exit_code == 1, checked.output
    payload = json.loads(checked.output)
    assert payload["first_failure_step"] == "enrich"
    [missing] = [f for f in payload["findings"] if f["type"] == "missing_field"]
    assert missing["node"] == "enrich"
    assert missing["severity"] == "critical"
    assert "respond" in missing["reason"]


def test_the_drop_without_consumers_does_not_fail():
    # The drop is invisible without a declared reader: the flag is what finds it.
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", "langsmith", str(DROP_FIXTURE)])
    assert ingested.exit_code == 0, ingested.output

    checked = runner.invoke(app, ["check", "last", "--format", "json"])
    assert checked.exit_code == 0, checked.output
    assert not [f for f in json.loads(checked.output)["findings"] if f["type"] == "missing_field"]


def test_consumers_file_of_the_wrong_shape_saves_nothing():
    # A bare string reader would be iterated letter by letter and match no step.
    Path("consumers.json").write_text(json.dumps({"customer_id": "respond"}))
    result = CliRunner().invoke(
        app, ["ingest", "langsmith", str(DROP_FIXTURE), "--consumers", "consumers.json"]
    )
    assert result.exit_code == 2, result.output
    assert "not a consumers file" in result.output
    assert list(Path(".argus/runs").iterdir()) == []


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
