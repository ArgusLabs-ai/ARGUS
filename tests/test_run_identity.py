"""Run identity: ``RunRecord.agent`` (which pipeline) and ``RunRecord.environment`` (where it ran).

``environment`` is set by the shared finalize, so every path gets it. ``agent`` is set on the
recorder path, including ``ArgusRecorder.bind`` which ``pytest --argus`` uses.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

import pytest
from conftest import make_event, make_run_record

from argus.storage import last_run_id, load_run, save_run
from argus.utils import ids

pytest.importorskip("langgraph")

from langgraph.graph import END, START, StateGraph  # noqa: E402

from argus.pytest_instrument import (  # noqa: E402
    install_auto_instrumentation,
    uninstall_auto_instrumentation,
)
from argus.recorder import ArgusRecorder  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    """Neither the developer's shell nor the CI runner may decide these tests."""
    for name in ("ARGUS_AGENT", "ARGUS_ENVIRONMENT", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)


_LANGSMITH_DEMO = Path(__file__).resolve().parent / "fixtures" / "langsmith" / "demo_graph.jsonl"


def _node_hash(names):
    return "g_" + hashlib.sha1(",".join(sorted(names)).encode()).hexdigest()[:10]


# ── environment ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_environment_defaults_to_local():
    assert ids.resolve_environment() == "local"


@pytest.mark.unit
def test_environment_is_ci_on_github_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ids.resolve_environment() == "ci"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["True", "1", "false", ""])
def test_only_the_exact_github_actions_value_means_ci(monkeypatch, value):
    monkeypatch.setenv("GITHUB_ACTIONS", value)
    assert ids.resolve_environment() == "local"


@pytest.mark.unit
def test_explicit_environment_beats_ci(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ARGUS_ENVIRONMENT", "prod")
    assert ids.resolve_environment() == "prod"


@pytest.mark.unit
def test_a_blank_environment_variable_counts_as_unset(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ARGUS_ENVIRONMENT", "   ")
    assert ids.resolve_environment() == "ci"


# ── agent: one test per precedence level ─────────────────────────────────────


@pytest.mark.unit
def test_agent_argument_wins_over_everything(monkeypatch):
    monkeypatch.setenv("ARGUS_AGENT", "from-env")
    agent = ids.resolve_agent(
        explicit="from-argument",
        graph_name="from-graph",
        app_factory_ref="pkg.mod:build",
        node_names=["a", "b"],
    )
    assert agent == "from-argument"


@pytest.mark.unit
def test_agent_env_beats_graph_name_factory_and_hash(monkeypatch):
    monkeypatch.setenv("ARGUS_AGENT", "from-env")
    agent = ids.resolve_agent(
        graph_name="from-graph", app_factory_ref="pkg.mod:build", node_names=["a"]
    )
    assert agent == "from-env"


@pytest.mark.unit
def test_agent_graph_name_beats_factory_and_hash():
    agent = ids.resolve_agent(
        graph_name="from-graph", app_factory_ref="pkg.mod:build", node_names=["a"]
    )
    assert agent == "from-graph"


@pytest.mark.unit
def test_langgraphs_default_graph_name_is_not_an_agent():
    agent = ids.resolve_agent(
        graph_name="LangGraph", app_factory_ref="pkg.graphs:build_app", node_names=["a"]
    )
    assert agent == "pkg.graphs"


@pytest.mark.unit
def test_agent_factory_module_beats_hash():
    agent = ids.resolve_agent(app_factory_ref="pkg.graphs:build_app", node_names=["a"])
    assert agent == "pkg.graphs"


@pytest.mark.unit
def test_agent_falls_back_to_a_hash_of_the_sorted_node_names():
    agent = ids.resolve_agent(graph_name="LangGraph", node_names=["reply", "fetch", "plan"])
    assert agent == _node_hash(["fetch", "plan", "reply"])
    assert agent == ids.resolve_agent(
        node_names=["plan", "reply", "fetch"]
    )  # order is not identity
    assert agent != ids.resolve_agent(node_names=["plan", "reply"])


@pytest.mark.unit
def test_blank_agent_values_count_as_unset(monkeypatch):
    monkeypatch.setenv("ARGUS_AGENT", "  ")
    agent = ids.resolve_agent(
        explicit=" ", graph_name="", app_factory_ref=":build", node_names=["a"]
    )
    assert agent == _node_hash(["a"])


@pytest.mark.unit
def test_a_graph_name_that_is_not_a_string_is_ignored():
    assert ids.resolve_agent(graph_name=None, node_names=["a"]) == _node_hash(["a"])
    assert ids.resolve_agent(graph_name=7, node_names=["a"]) == _node_hash(["a"])  # type: ignore[arg-type]


# ── storage ──────────────────────────────────────────────────────────────────


def _edit_run_file(run_id, mutate, **record_fields):
    """Save a run, then rewrite its JSON the way an older or hand-edited file looks."""
    record = make_run_record(events=[make_event()], run_id=run_id)
    for key, value in record_fields.items():
        setattr(record, key, value)
    path = save_run(record)
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_run(run_id)


@pytest.mark.unit
def test_agent_and_environment_survive_a_save_and_load():
    loaded = _edit_run_file(
        "ident-roundtrip", lambda data: None, agent="support", environment="ci"
    )
    assert (loaded.agent, loaded.environment) == ("support", "ci")


@pytest.mark.unit
def test_a_new_record_has_neither_until_something_sets_them():
    record = make_run_record()
    assert record.agent is None
    assert record.environment is None


@pytest.mark.unit
def test_run_file_from_before_these_fields_still_loads():
    def drop(data):
        data.pop("agent", None)
        data.pop("environment", None)

    loaded = _edit_run_file("ident-old", drop, agent="support", environment="ci")
    assert loaded.agent is None
    assert loaded.environment is None


@pytest.mark.unit
def test_a_hand_edited_value_that_is_not_text_loads_as_none():
    def mangle(data):
        data["agent"] = 7
        data["environment"] = ["ci"]

    loaded = _edit_run_file("ident-mangled", mangle)
    assert loaded.agent is None
    assert loaded.environment is None


# ── the shared finalize sets environment on every path ───────────────────────


class _S(TypedDict, total=False):
    n: int


def _compiled(name=None):
    graph = StateGraph(_S)
    graph.add_node("fetch", lambda state: {"n": 1})
    graph.add_node("reply", lambda state: {"n": 2})
    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "reply")
    graph.add_edge("reply", END)
    return graph.compile() if name is None else graph.compile(name=name)


def _recorded(compiled, **recorder_kwargs):
    recorder = ArgusRecorder(semantic_judge=False, **recorder_kwargs)
    recorder.attach(compiled).invoke({"n": 0})
    return load_run(recorder.run_ids[-1])


@pytest.mark.unit
def test_the_legacy_session_path_gets_an_environment(monkeypatch):
    from argus.session import ArgusSession

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    session = ArgusSession()
    session.set_node_names(["a"])
    session.set_edges({"a": []})
    session.wrap("a", lambda state: {"x": 1})({})
    session.finalize()
    loaded = load_run(session.run_id)
    assert loaded.environment == "ci"
    assert loaded.agent is None  # only the recorder path knows which pipeline this is


@pytest.mark.unit
def test_a_trace_ingest_gets_an_environment(monkeypatch):
    from argus.ingest.langsmith import ingest_langsmith

    monkeypatch.setattr("argus.cloud.is_logged_in", lambda: False)
    monkeypatch.setenv("ARGUS_ENVIRONMENT", "staging")
    session = ingest_langsmith(_LANGSMITH_DEMO)
    assert load_run(session.run_id).environment == "staging"


@pytest.mark.integration
def test_the_recorder_path_gets_an_environment(monkeypatch):
    assert _recorded(_compiled()).environment == "local"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _recorded(_compiled()).environment == "ci"
    monkeypatch.setenv("ARGUS_ENVIRONMENT", "prod")
    assert _recorded(_compiled()).environment == "prod"


# ── the recorder path sets agent ─────────────────────────────────────────────


@pytest.mark.integration
def test_recorder_agent_argument_wins(monkeypatch):
    monkeypatch.setenv("ARGUS_AGENT", "from-env")
    record = _recorded(_compiled(name="from-graph"), agent="from-argument")
    assert record.agent == "from-argument"


@pytest.mark.integration
def test_recorder_reads_the_env_variable(monkeypatch):
    monkeypatch.setenv("ARGUS_AGENT", "from-env")
    assert _recorded(_compiled(name="from-graph")).agent == "from-env"


@pytest.mark.integration
def test_recorder_uses_the_compiled_graphs_name():
    assert _recorded(_compiled(name="refund-agent")).agent == "refund-agent"


@pytest.mark.integration
def test_recorder_skips_langgraphs_default_name_for_a_node_hash():
    assert _recorded(_compiled()).agent == _node_hash(["fetch", "reply"])


@pytest.mark.integration
def test_one_recorder_names_every_run_it_serves_the_same():
    recorder = ArgusRecorder(semantic_judge=False)
    app = recorder.attach(_compiled(name="refund-agent"))
    app.invoke({"n": 0})
    app.invoke({"n": 1})
    assert [load_run(r).agent for r in recorder.run_ids] == ["refund-agent", "refund-agent"]


# ── pytest --argus binds the recorder without attach(); it must resolve the same ──


@pytest.fixture
def auto_wrap():
    install_auto_instrumentation()
    try:
        yield
    finally:
        uninstall_auto_instrumentation()


def _auto_recorded_agent(compiled):
    compiled.invoke({"n": 0})
    run_id = last_run_id()
    assert run_id is not None
    return load_run(run_id).agent


@pytest.mark.integration
def test_bind_uses_the_compiled_graphs_name(auto_wrap):
    assert _auto_recorded_agent(_compiled(name="auto-named")) == "auto-named"


@pytest.mark.integration
def test_bind_reads_the_env_variable(auto_wrap, monkeypatch):
    monkeypatch.setenv("ARGUS_AGENT", "from-env")
    assert _auto_recorded_agent(_compiled(name="auto-named")) == "from-env"


@pytest.mark.integration
def test_bind_falls_back_to_the_node_hash(auto_wrap):
    assert _auto_recorded_agent(_compiled()) == _node_hash(["fetch", "reply"])


@pytest.mark.integration
def test_bind_on_a_bare_recorder_resolves_like_attach():
    compiled = _compiled(name="bound")
    recorder = ArgusRecorder(semantic_judge=False, agent=None)
    recorder.bind(compiled)
    compiled.invoke({"n": 0}, {"callbacks": [recorder]})
    assert load_run(recorder.run_ids[-1]).agent == "bound"
