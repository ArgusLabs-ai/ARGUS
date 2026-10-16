"""Run completeness: `RunRecord.complete` / `end_reason` (split from #171)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_event, make_run_record

from argus.storage import load_run, save_run


def _edit_run_file(run_id, mutate, **record_fields):
    """Save a run, then rewrite its JSON the way an older/hand-edited file looks."""
    record = make_run_record(events=[make_event()], run_id=run_id)
    for key, value in record_fields.items():
        setattr(record, key, value)
    path = save_run(record)
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_run(run_id)


def _drop_completeness(data):
    data.pop("complete", None)
    data.pop("end_reason", None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "interrupted", "complete", "end_reason"),
    [
        ("crashed", False, False, "exception"),
        ("interrupted", False, False, "interrupted"),
        ("clean", True, False, "interrupted"),  # only the flag survived
        ("crashed", True, False, "exception"),  # a crash outranks an interrupt, as in session
        ("clean", False, True, None),
        ("silent_failure", False, True, None),
    ],
)
def test_old_run_file_derives_completeness_from_how_it_ended(
    status, interrupted, complete, end_reason
):
    loaded = _edit_run_file(
        "old-derive", _drop_completeness, overall_status=status, interrupted=interrupted
    )
    assert loaded.complete is complete
    assert loaded.end_reason == end_reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "complete", "end_reason"),
    [
        ("crashed", True, "finished"),  # carries the key, so it is not second-guessed
        ("clean", False, "exception"),
        ("interrupted", True, None),
    ],
)
def test_run_file_that_carries_complete_is_not_rederived(status, complete, end_reason):
    loaded = _edit_run_file(
        "new-format",
        lambda data: None,
        overall_status=status,
        complete=complete,
        end_reason=end_reason,
    )
    assert loaded.complete is complete
    assert loaded.end_reason == end_reason


def _unfinished_session():
    """A session that ran node ``a`` and was never told the run ended (``b`` never ran)."""
    from argus.session import ArgusSession

    session = ArgusSession()
    session.set_node_names(["a", "b"])
    session.set_edges({"a": ["b"], "b": []})
    session.wrap("a", lambda state: {"x": 1})({})
    return session


@pytest.mark.unit
def test_atexit_finalize_saves_a_run_nobody_ended_as_partial():
    import atexit

    session = _unfinished_session()
    try:
        session._atexit_finalize()  # what the interpreter calls at exit
        loaded = load_run(session.run_id)
        assert loaded.complete is False
        assert loaded.end_reason == "unknown"
    finally:
        atexit.unregister(session._atexit_finalize)  # even when an assert above fails


@pytest.mark.unit
def test_explicit_finalize_after_the_run_returned_stays_complete():
    session = _unfinished_session()
    session.finalize()
    loaded = load_run(session.run_id)
    assert loaded.complete is True
    assert loaded.end_reason == "finished"


def _recorded_loop(limit):
    """Record a one-node loop run under ``recursion_limit=limit``; returns (error, run)."""
    pytest.importorskip("langgraph")
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder

    class State(TypedDict, total=False):
        n: int

    graph = StateGraph(State)
    graph.add_node("inc", lambda state: {"n": state.get("n", 0) + 1})
    graph.add_edge(START, "inc")
    graph.add_conditional_edges(
        "inc", lambda state: "inc" if state["n"] < 3 else "done", {"inc": "inc", "done": END}
    )
    recorder = ArgusRecorder()
    app = recorder.attach(graph.compile())
    error = None
    try:
        app.invoke({"n": 0}, {"recursion_limit": limit})
    except Exception as exc:  # the run under test is the one that raises
        error = exc
    return error, load_run(recorder.session.run_id)


@pytest.mark.integration
def test_graph_that_raises_outside_every_node_is_saved_partial():
    # GraphRecursionError comes from the graph loop itself: every node returned fine
    error, record = _recorded_loop(limit=2)
    assert type(error).__name__ == "GraphRecursionError"
    assert all(e.status != "crashed" for e in record.steps)
    assert record.complete is False
    assert record.end_reason == "exception"


@pytest.mark.integration
def test_graph_that_returns_normally_is_saved_complete():
    error, record = _recorded_loop(limit=25)
    assert error is None
    assert record.complete is True
    assert record.end_reason == "finished"


_LANGSMITH_DEMO = Path(__file__).resolve().parent / "fixtures" / "langsmith" / "demo_graph.jsonl"


def _write_trace(path, *, root_error):
    """The demo trace with every node healthy, and the root run's ``error`` as given."""
    rows = [json.loads(line) for line in _LANGSMITH_DEMO.read_text().splitlines() if line.strip()]
    for row in rows:
        if row["name"] == "summarize":
            row["outputs"] = {"summary": "docs summarised"}
        if row["parent_run_id"] is None:
            row["error"] = root_error
            if root_error:
                row["outputs"] = None  # a root that raised has no final state
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


@pytest.mark.unit
def test_ingested_root_error_with_healthy_nodes_is_saved_partial(tmp_path, monkeypatch):
    from argus.ingest.langsmith import ingest_langsmith

    monkeypatch.setattr("argus.cloud.is_logged_in", lambda: False)
    trace = _write_trace(tmp_path / "trace.jsonl", root_error="GraphRecursionError: limit hit")
    session = ingest_langsmith(trace)
    record = load_run(session.run_id)
    assert all(e.status != "crashed" for e in record.steps)
    assert record.complete is False
    assert record.end_reason == "exception"


@pytest.mark.unit
def test_ingested_trace_without_a_root_error_is_saved_complete(tmp_path, monkeypatch):
    from argus.ingest.langsmith import ingest_langsmith

    monkeypatch.setattr("argus.cloud.is_logged_in", lambda: False)
    session = ingest_langsmith(_write_trace(tmp_path / "trace.jsonl", root_error=None))
    record = load_run(session.run_id)
    assert record.complete is True
    assert record.end_reason == "finished"


def _ran_at_exit(*, names=None, edges=None, conditional=None, ran=()):
    """Run the named nodes on a standalone session, then fire its atexit hook.

    Auto-finalize is deferred, as it is for every recorder session: otherwise the node
    wrapper would end a run that reached its last terminal before the hook ever ran.
    """
    import atexit

    from argus.session import ArgusSession

    session = ArgusSession()
    session._defer_auto_finalize = True
    if names is not None:
        session.set_node_names(names)
    if edges is not None:
        session.set_edges(edges)
    if conditional is not None:
        session.set_conditional_sources(set(conditional))
    try:
        for name in ran:
            session.wrap(name, lambda state: {"x": 1})({})
        session._atexit_finalize()
        return load_run(session.run_id)
    finally:
        atexit.unregister(session._atexit_finalize)


_AB = {"a": ["b"], "b": []}


@pytest.mark.unit
def test_atexit_edges_only_run_that_reached_every_leaf_is_complete():
    record = _ran_at_exit(edges=_AB, ran=["a", "b"])
    assert (record.complete, record.end_reason) == (True, "finished")


@pytest.mark.unit
def test_atexit_edges_only_run_that_missed_a_leaf_is_unknown():
    record = _ran_at_exit(edges=_AB, ran=["a"])
    assert (record.complete, record.end_reason) == (False, "unknown")


@pytest.mark.unit
def test_atexit_edges_only_fan_out_needs_every_leaf():
    edges = {"a": ["b", "c"], "b": [], "c": []}
    assert _ran_at_exit(edges=edges, ran=["a", "b", "c"]).complete is True
    assert _ran_at_exit(edges=edges, ran=["a", "b"]).complete is False


@pytest.mark.unit
def test_atexit_names_only_run_is_unknown_until_every_node_ran():
    record = _ran_at_exit(names=["a", "b"], ran=["a"])
    assert (record.complete, record.end_reason) == (False, "unknown")
    assert _ran_at_exit(names=["a", "b"], ran=["a", "b"]).complete is True


@pytest.mark.unit
def test_atexit_run_with_no_topology_is_unknown():
    record = _ran_at_exit(ran=["a"])
    assert (record.complete, record.end_reason) == (False, "unknown")


@pytest.mark.unit
def test_atexit_cyclic_run_that_reached_its_terminal_is_complete():
    names, edges = ["a", "b", "c"], {"a": ["b"], "b": ["a", "c"], "c": []}
    assert _ran_at_exit(names=names, edges=edges, ran=["a", "b", "c"]).complete is True
    unfinished = _ran_at_exit(names=names, edges=edges, ran=["a", "b", "a", "b"])
    assert (unfinished.complete, unfinished.end_reason) == (False, "unknown")


@pytest.mark.unit
@pytest.mark.parametrize("reset", ["begin_new_run", "reset_for_resume"])
def test_a_new_run_starts_without_the_previous_runs_root_error(reset):
    import atexit

    from argus.session import ArgusSession

    session = ArgusSession()
    try:
        session._root_error = RuntimeError("the last run died")
        getattr(session, reset)(*(["parent-run"] if reset == "reset_for_resume" else []))
        assert session._root_error is None
    finally:
        atexit.unregister(session._atexit_finalize)  # else it saves a run at pytest exit


_WATCH_KW = dict(semantic_judge=False, investigate=False, record_http=False)


def _watched(build):
    """(watcher, compiled app) for a graph built by ``build()`` and watched via the builder."""
    pytest.importorskip("langgraph")
    from argus.watcher import ArgusWatcher

    graph = build()
    watcher = ArgusWatcher(graph, **_WATCH_KW)
    return watcher, graph.compile()


def _loop_graph():
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict, total=False):
        n: int

    graph = StateGraph(State)
    graph.add_node("inc", lambda state: {"n": state.get("n", 0) + 1})
    graph.add_edge(START, "inc")
    graph.add_conditional_edges(
        "inc", lambda state: "inc" if state["n"] < 3 else "done", {"inc": "inc", "done": END}
    )
    return graph


def _line_graph(router=None):
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict, total=False):
        n: int

    graph = StateGraph(State)
    for name in ("a", "b", "c"):
        graph.add_node(name, lambda state: {"n": state.get("n", 0) + 1})
    graph.add_edge(START, "a")
    if router is None:
        graph.add_edge("a", "b")
    else:
        graph.add_conditional_edges("a", router, {"b": "b"})
    graph.add_edge("b", "c")
    graph.add_edge("c", END)
    return graph


def _saved(watcher):
    return load_run(watcher._session.run_id)


def _run_async(coroutine):
    """Run a coroutine in a worker thread, so its event loop never becomes this thread's.

    LangGraph leaves the policy's current loop set to a closed one; other tests
    in the suite call ``asyncio.get_event_loop()`` on the main thread.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


@pytest.mark.integration
def test_watcher_run_that_hits_the_recursion_limit_is_saved_partial():
    watcher, app = _watched(_loop_graph)
    with pytest.raises(Exception) as caught:
        app.invoke({"n": 0}, {"recursion_limit": 2})
    assert type(caught.value).__name__ == "GraphRecursionError"
    record = _saved(watcher)
    assert all(e.status != "crashed" for e in record.steps)
    assert (record.complete, record.end_reason) == (False, "exception")
    assert record.overall_status == "clean"  # the root error is not a node grade


@pytest.mark.integration
def test_watcher_run_whose_router_raises_is_saved_partial():
    def router(state):
        raise RuntimeError("router boom")

    watcher, app = _watched(lambda: _line_graph(router))
    with pytest.raises(RuntimeError, match="router boom"):
        app.invoke({"n": 0})
    record = _saved(watcher)
    assert all(e.status != "crashed" for e in record.steps)
    assert (record.complete, record.end_reason) == (False, "exception")


@pytest.mark.integration
def test_watcher_run_that_streams_into_an_error_is_saved_partial():
    watcher, app = _watched(_loop_graph)
    with pytest.raises(Exception) as caught:
        list(app.stream({"n": 0}, {"recursion_limit": 2}))
    assert type(caught.value).__name__ == "GraphRecursionError"
    assert (_saved(watcher).complete, _saved(watcher).end_reason) == (False, "exception")


@pytest.mark.integration
def test_watcher_async_run_that_hits_the_recursion_limit_is_saved_partial():
    watcher, app = _watched(_loop_graph)

    async def run():
        await app.ainvoke({"n": 0}, {"recursion_limit": 2})

    with pytest.raises(Exception) as caught:
        _run_async(run())
    assert type(caught.value).__name__ == "GraphRecursionError"
    assert (_saved(watcher).complete, _saved(watcher).end_reason) == (False, "exception")


@pytest.mark.integration
def test_watcher_stream_abandoned_after_the_first_chunk_is_saved_cancelled():
    watcher, app = _watched(_line_graph)
    stream = app.stream({"n": 0})
    next(stream)  # the first node ran; b and c never will
    stream.close()
    record = _saved(watcher)
    assert [e.node_name for e in record.steps] == ["a"]
    assert (record.complete, record.end_reason) == (False, "cancelled")
    assert record.overall_status == "clean"


@pytest.mark.integration
def test_watcher_stream_dropped_without_closing_is_saved_cancelled():
    watcher, app = _watched(_line_graph)
    stream = app.stream({"n": 0})
    next(stream)
    del stream  # the consumer just lets go of it
    assert (_saved(watcher).complete, _saved(watcher).end_reason) == (False, "cancelled")


@pytest.mark.integration
def test_watcher_astream_abandoned_after_the_first_chunk_is_saved_cancelled():
    watcher, app = _watched(_line_graph)

    async def run():
        stream = app.astream({"n": 0})
        await stream.__anext__()
        await stream.aclose()

    _run_async(run())
    assert (_saved(watcher).complete, _saved(watcher).end_reason) == (False, "cancelled")


@pytest.mark.integration
def test_watcher_run_that_finishes_normally_stays_complete():
    watcher, app = _watched(_line_graph)
    app.invoke({"n": 0})
    assert (_saved(watcher).complete, _saved(watcher).end_reason) == (True, "finished")


@pytest.mark.integration
def test_watcher_stream_that_is_fully_consumed_stays_complete():
    watcher, app = _watched(_line_graph)
    assert len(list(app.stream({"n": 0}))) == 3
    assert (_saved(watcher).complete, _saved(watcher).end_reason) == (True, "finished")


@pytest.mark.integration
def test_watcher_stream_abandoned_after_the_last_chunk_stays_complete():
    watcher, app = _watched(_line_graph)
    stream = app.stream({"n": 0})
    for _ in range(3):  # every node has run; the consumer just never asks for the end
        next(stream)
    stream.close()
    assert [e.node_name for e in _saved(watcher).steps] == ["a", "b", "c"]
    assert (_saved(watcher).complete, _saved(watcher).end_reason) == (True, "finished")


@pytest.mark.integration
def test_watcher_reused_after_a_failed_run_saves_the_next_run_complete():
    watcher, app = _watched(_loop_graph)
    with pytest.raises(Exception):
        app.invoke({"n": 0}, {"recursion_limit": 2})
    failed = _saved(watcher)
    assert failed.complete is False

    app.invoke({"n": 0}, {"recursion_limit": 25})
    again = _saved(watcher)
    assert again.run_id != failed.run_id
    assert (again.complete, again.end_reason) == (True, "finished")


@pytest.mark.unit
def test_watcher_does_not_call_a_graph_interrupt_a_root_error():
    import atexit

    from argus.session import ArgusSession, _GraphInterrupt
    from argus.watcher import ArgusWatcher

    if _GraphInterrupt is None:
        pytest.skip("langgraph is not installed")
    watcher = ArgusWatcher(**_WATCH_KW)
    session = watcher._session = ArgusSession()
    try:
        watcher._note_root_error(_GraphInterrupt())  # a human-in-the-loop pause, not a failure
        assert session._root_error is None
        watcher._note_root_error(RuntimeError("boom"))
        assert isinstance(session._root_error, RuntimeError)
    finally:
        atexit.unregister(session._atexit_finalize)  # else it saves a run at pytest exit


def _unmapped_router_graph():
    """start -> R, where R routes through a path function with no path map (branches unknown)."""
    pytest.importorskip("langgraph")
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict, total=False):
        n: int

    graph = StateGraph(State)
    for name in ("start", "R", "X", "Y"):
        graph.add_node(name, lambda state: {"n": 1})
    graph.add_edge(START, "start")
    graph.add_edge("start", "R")
    graph.add_conditional_edges("R", lambda state: "X")
    graph.add_edge("X", END)
    graph.add_edge("Y", END)
    return graph


def _unknown(record):
    return (record.complete, record.end_reason) == (False, "unknown")


@pytest.mark.unit
def test_atexit_a_router_with_unknown_branches_is_not_a_leaf():
    # the patcher reports a router whose targets it cannot read as "R": [], flagged conditional
    record = _ran_at_exit(edges={"start": ["R"], "R": []}, conditional={"R"}, ran=["start", "R"])
    assert _unknown(record)


@pytest.mark.integration
def test_atexit_the_patchers_own_output_for_an_unmapped_router_reads_as_unfinished():
    from argus.patcher import extract_conditional_sources, extract_edge_map

    graph = _unmapped_router_graph()
    edges, conditional = extract_edge_map(graph), extract_conditional_sources(graph)
    assert edges["R"] == [] and conditional == {"R"}  # the premise: no successors, but flagged
    for names in (None, list(graph.nodes)):  # edges-only, and with the node list the watcher adds
        record = _ran_at_exit(
            names=names, edges=edges, conditional=conditional, ran=["start", "R"]
        )
        assert _unknown(record)


@pytest.mark.unit
def test_atexit_a_never_run_parallel_branch_blocks_complete_on_a_conditional_graph():
    # start -> R (conditional -> X) and start -> B -> B2 -> Z; the run stopped after X
    names = ["start", "R", "X", "B", "B2", "Z"]
    edges = {"start": ["R", "B"], "R": ["X"], "B": ["B2"], "B2": ["Z"], "X": [], "Z": []}
    record = _ran_at_exit(names=names, edges=edges, conditional={"R"}, ran=["start", "R", "X"])
    assert _unknown(record)


@pytest.mark.unit
def test_atexit_a_conditional_graph_is_never_called_complete():
    # no rule can tell a chosen branch from an unrun one (or count a Send fan-out), so unknown
    names, edges = ["start", "R", "X", "Y"], {"start": ["R"], "R": ["X", "Y"], "X": [], "Y": []}
    for given in (None, names):
        record = _ran_at_exit(names=given, edges=edges, conditional={"R"}, ran=["start", "R", "X"])
        assert _unknown(record)


@pytest.mark.unit
def test_atexit_edges_that_end_at_the_end_sentinel_are_complete_once_every_leaf_ran():
    edges = {"A": ["B"], "B": ["__end__"]}
    assert _ran_at_exit(edges=edges, ran=["A", "B"]).complete is True
    assert _unknown(_ran_at_exit(edges=edges, ran=["A"]))

    framed = {"__start__": ["A"], "A": ["B", "C"], "B": ["__end__"], "C": ["__end__"]}
    assert _ran_at_exit(edges=framed, ran=["A", "B", "C"]).complete is True
    assert _unknown(_ran_at_exit(edges=framed, ran=["A", "B"]))


@pytest.mark.unit
def test_atexit_an_edge_map_of_only_sentinels_has_no_leaf_to_reach():
    assert _unknown(_ran_at_exit(edges={"__start__": ["__end__"]}, ran=["a"]))


@pytest.mark.unit
def test_a_keyboard_interrupt_is_saved_cancelled_not_exception():
    session = _unfinished_session()
    session._root_error = KeyboardInterrupt()
    session.finalize()
    record = load_run(session.run_id)
    assert (record.complete, record.end_reason) == (False, "cancelled")


@pytest.mark.integration
def test_watcher_run_the_user_aborts_with_ctrl_c_is_saved_cancelled():
    def router(state):
        raise KeyboardInterrupt

    watcher, app = _watched(lambda: _line_graph(router))
    with pytest.raises(KeyboardInterrupt):
        app.invoke({"n": 0})
    record = _saved(watcher)
    assert (record.complete, record.end_reason) == (False, "cancelled")


@pytest.mark.unit
def test_atexit_edges_only_run_that_skipped_a_branch_node_is_unknown():
    # a -> b -> d and a -> c -> d; the run took one branch: every leaf (d) ran, b never did
    edges = {"a": ["b", "c"], "b": ["d"], "c": ["d"]}
    assert _unknown(_ran_at_exit(edges=edges, ran=["a", "c", "d"]))
    record = _ran_at_exit(edges=edges, ran=["a", "b", "c", "d"])
    assert (record.complete, record.end_reason) == (True, "finished")


@pytest.mark.unit
def test_atexit_edges_only_skipped_node_is_found_behind_the_sentinels_too():
    framed = {
        "__start__": ["a"],
        "a": ["b", "c"],
        "b": ["d"],
        "c": ["d"],
        "d": ["__end__"],
    }
    assert _unknown(_ran_at_exit(edges=framed, ran=["a", "c", "d"]))
    assert _ran_at_exit(edges=framed, ran=["a", "c", "b", "d"]).complete is True


@pytest.mark.unit
def test_atexit_edges_only_linear_fan_out_and_cyclic_runs_that_ran_every_node_stay_complete():
    assert _ran_at_exit(edges={"a": ["b"], "b": ["c"], "c": []}, ran=["a", "b", "c"]).complete
    assert _ran_at_exit(edges={"a": ["b", "c"], "b": [], "c": []}, ran=["a", "c", "b"]).complete
    cyclic = {"a": ["b"], "b": ["a", "c"], "c": []}
    assert _ran_at_exit(edges=cyclic, ran=["a", "b", "a", "b", "c"]).complete is True
    assert _unknown(_ran_at_exit(edges=cyclic, ran=["a", "b", "a", "b"]))


@pytest.mark.unit
def test_atexit_upper_case_sentinels_are_not_nodes():
    edges = {"__START__": ["A"], "A": ["B"], "B": ["__END__"]}
    assert _ran_at_exit(edges=edges, ran=["A", "B"]).complete is True
    assert _unknown(_ran_at_exit(edges=edges, ran=["A"]))


@pytest.mark.unit
def test_atexit_start_and_end_are_real_node_names_not_sentinels():
    edges = {"start": ["A"], "A": ["END"], "END": []}
    assert _unknown(_ran_at_exit(edges=edges, ran=["start", "A"]))  # END never ran
    assert _ran_at_exit(edges=edges, ran=["start", "A", "END"]).complete is True
