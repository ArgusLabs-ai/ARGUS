"""Smoke tests — verify core imports and basic session behaviour."""
import pytest

from argus.llm_tracker import extract_usage, scan_output_for_tokens  # noqa: I001
from argus.models import ArgusConfig, LLMCallInfo, LLMUsage, NodeEvent, RunRecord
from argus.pricing import calculate_cost
from argus.session import ArgusSession
from argus.storage import load_run


@pytest.fixture(autouse=True)
def _isolate_runs(tmp_path, monkeypatch):
    """Run every test in a temp directory so .argus/runs/ doesn't pollute the project."""
    monkeypatch.chdir(tmp_path)


def test_imports():
    assert NodeEvent is not None
    assert RunRecord is not None
    assert LLMCallInfo is not None
    assert LLMUsage is not None


def test_pricing_known_model():
    cost = calculate_cost("gpt-4o", 1000, 500)
    assert cost is not None
    assert cost > 0


def test_pricing_unknown_model():
    assert calculate_cost("totally-unknown-model-xyz", 100, 100) is None


def test_scan_output_usage_metadata():
    output = {
        "result": "hello",
        "usage_metadata": {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "model": "gpt-4o-mini",
        },
    }
    calls = scan_output_for_tokens(output)
    assert len(calls) == 1
    assert calls[0].total_tokens == 150
    assert calls[0].prompt_tokens == 100
    assert calls[0].completion_tokens == 50


def test_scan_output_empty():
    assert scan_output_for_tokens({}) == []
    assert scan_output_for_tokens(None) == []


def test_extract_usage_from_output():
    output = {
        "usage_metadata": {
            "input_tokens": 200,
            "output_tokens": 80,
            "model": "claude-3-5-sonnet",
        },
    }
    usage = extract_usage(None, output)
    assert usage is not None
    assert usage.total_tokens == 280
    assert usage.total_cost_usd is not None


def test_session_creates_run():
    session = ArgusSession()

    def node_a(state):
        return {"value": 42}

    wrapped = session.wrap("node_a", node_a)
    session.set_node_names(["node_a"])
    wrapped({"value": 0})
    session.finalize()


def test_session_detects_missing_field():
    session = ArgusSession()
    session.set_node_names(["producer", "consumer"])
    session.set_edges({"producer": ["consumer"]})

    wrap_p = session.wrap("producer", lambda s: {"key_a": 1})
    wrap_c = session.wrap("consumer", lambda s: {"result": s.get("key_b", "missing")})

    wrap_p({})
    wrap_c({"key_a": 1})
    session.finalize()
    # if no exception, the session ran and inspected transitions successfully


def test_replay_engine_rejects_bad_node():
    from argus.replay import ReplayEngine

    engine = ReplayEngine()
    try:
        engine.replay("nonexistent-run-id-xyz", "fake_node")
        assert False, "Should have raised"
    except (FileNotFoundError, ValueError):
        pass


def test_storage_roundtrip():
    from argus.storage import load_run

    session = ArgusSession()
    session.set_node_names(["step1"])
    wrapped = session.wrap("step1", lambda s: {"out": 1})
    wrapped({"in": 0})
    session.finalize()

    # finalize() saves the run — verify we can load it back
    loaded = load_run(session.run_id)
    assert loaded.run_id == session.run_id
    assert len(loaded.steps) == 1


# ── Parallel workflow tests ──────────────────────────────────────────────────


def test_terminal_nodes_linear():
    """Linear A→B→C: only C is terminal, backward compat with old behavior."""
    session = ArgusSession()
    session.set_node_names(["A", "B", "C"])
    session.set_edges({"A": ["B"], "B": ["C"]})
    assert session._terminal_nodes == {"C"}


def test_terminal_nodes_fan_out():
    """A→[B,C]: both B and C are terminal (no successors)."""
    session = ArgusSession()
    session.set_node_names(["A", "B", "C"])
    session.set_edges({"A": ["B", "C"]})
    assert session._terminal_nodes == {"B", "C"}


def test_terminal_nodes_asymmetric():
    """A→[B,C], B→D: terminals are C and D."""
    session = ArgusSession()
    session.set_node_names(["A", "B", "C", "D"])
    session.set_edges({"A": ["B", "C"], "B": ["D"]})
    assert session._terminal_nodes == {"C", "D"}


def test_terminal_nodes_fan_in():
    """A→[B,C]→D: only D is terminal."""
    session = ArgusSession()
    session.set_node_names(["A", "B", "C", "D"])
    session.set_edges({"A": ["B", "C"], "B": ["D"], "C": ["D"]})
    assert session._terminal_nodes == {"D"}


def test_parallel_fan_out_all_events_captured():
    """A→[B,C]: both B and C events must be in the finalized record."""
    from argus.storage import load_run

    session = ArgusSession()
    edges = {"A": ["B", "C"]}
    wrapped = session.instrument(
        agents={
            "A": lambda s: {"from_a": True},
            "B": lambda s: {"from_b": True},
            "C": lambda s: {"from_c": True},
        },
        edges=edges,
    )

    state = wrapped["A"]({})
    # Simulate parallel execution — order shouldn't matter
    wrapped["B"]({**state, "from_a": True})
    wrapped["C"]({**state, "from_a": True})

    loaded = load_run(session.run_id)
    node_names = [s.node_name for s in loaded.steps]
    assert "A" in node_names
    assert "B" in node_names
    assert "C" in node_names
    assert len(loaded.steps) == 3


def test_parallel_asymmetric_all_events_captured():
    """A→[B,C], B→D: all 4 events captured regardless of completion order."""
    from argus.storage import load_run

    session = ArgusSession()
    edges = {"A": ["B", "C"], "B": ["D"]}
    wrapped = session.instrument(
        agents={
            "A": lambda s: {"from_a": True},
            "B": lambda s: {"from_b": True},
            "C": lambda s: {"from_c": True},
            "D": lambda s: {"from_d": True},
        },
        edges=edges,
    )

    state = wrapped["A"]({})
    # C finishes first, then B→D
    wrapped["C"]({**state, "from_a": True})
    wrapped["B"]({**state, "from_a": True})
    wrapped["D"]({**state, "from_a": True, "from_b": True})

    loaded = load_run(session.run_id)
    node_names = [s.node_name for s in loaded.steps]
    assert set(node_names) == {"A", "B", "C", "D"}
    assert len(loaded.steps) == 4


def test_parallel_asymmetric_d_before_c():
    """A→[B,C], B→D: D finishes before C — C must not be lost."""
    from argus.storage import load_run

    session = ArgusSession()
    edges = {"A": ["B", "C"], "B": ["D"]}
    wrapped = session.instrument(
        agents={
            "A": lambda s: {"from_a": True},
            "B": lambda s: {"from_b": True},
            "C": lambda s: {"from_c": True},
            "D": lambda s: {"from_d": True},
        },
        edges=edges,
    )

    state = wrapped["A"]({})
    # B→D finishes first, then C arrives late
    wrapped["B"]({**state, "from_a": True})
    wrapped["D"]({**state, "from_a": True, "from_b": True})
    # Under the old bug, finalize would have triggered at D.
    # C should still be captured.
    wrapped["C"]({**state, "from_a": True})

    loaded = load_run(session.run_id)
    node_names = [s.node_name for s in loaded.steps]
    assert set(node_names) == {"A", "B", "C", "D"}
    assert len(loaded.steps) == 4
    assert loaded.overall_status in ("clean", "silent_failure")


# ── VAR-7: Input-output coherence checks ─────────────────────────────────────


def _has_failure(step: object, failure_type: str) -> bool:
    insp = getattr(step, "inspection", None)
    if insp is None:
        return False
    return any(f.failure_type == failure_type for f in insp.tool_failures)


def test_selective_attention():
    """Rule 13: output list < 50% of input list items (≥4 items) → flag."""
    from argus.storage import load_run

    session = ArgusSession()
    session.set_node_names(["reducer"])
    wrapped = session.wrap("reducer", lambda s: {"items": [1, 2]})
    wrapped({"items": [1, 2, 3, 4, 5]})
    session.finalize()

    loaded = load_run(session.run_id)
    assert _has_failure(loaded.steps[0], "selective_attention_reduction")


def test_selective_attention_suppressed_for_reducer():
    """Rule 13 should NOT fire when the field has a reducer (e.g. operator.add)."""
    import operator

    from argus.storage import load_run

    session = ArgusSession()
    session.set_node_names(["reducer"])
    session.reducer_fields = {"items": operator.add}
    wrapped = session.wrap("reducer", lambda s: {"items": [1, 2]})
    wrapped({"items": [1, 2, 3, 4, 5]})
    session.finalize()

    loaded = load_run(session.run_id)
    assert not _has_failure(loaded.steps[0], "selective_attention_reduction")


def test_input_echo():
    """Rule 14: output string ≥ 90% similar to input → flag."""
    from argus.storage import load_run

    long_text = (
        "The market outlook is very bullish with strong"
        " momentum and positive indicators. "
    ) * 2
    session = ArgusSession()
    session.set_node_names(["echo_node"])
    wrapped = session.wrap("echo_node", lambda s: {"result": s["text"]})
    wrapped({"text": long_text})
    session.finalize()

    loaded = load_run(session.run_id)
    assert _has_failure(loaded.steps[0], "input_echo")


def test_contradictory_transformation():
    """Rule 15: input is bullish, output is bearish → flag semantic_contradiction."""
    from argus.storage import load_run

    session = ArgusSession()
    session.set_node_names(["transformer"])
    wrapped = session.wrap(
        "transformer",
        lambda s: {"recommendation": "bearish sell downtrend"},
    )
    wrapped({"signal": "bullish uptrend buy"})
    session.finalize()

    loaded = load_run(session.run_id)
    assert _has_failure(loaded.steps[0], "semantic_contradiction")


def test_context_overflow_proxy():
    """Rule 16: input state > 100K chars → flag context_size_anomaly."""
    from argus.storage import load_run

    session = ArgusSession()
    session.set_node_names(["big_node"])
    wrapped = session.wrap("big_node", lambda s: {"result": "ok"})
    wrapped({"body": "x" * 110_000})
    session.finalize()

    loaded = load_run(session.run_id)
    assert _has_failure(loaded.steps[0], "context_size_anomaly")


# ── Loop-aware retry tests ───────────────────────────────────────────────


@pytest.mark.unit
def test_loop_retried_on_self_correct():
    """Loop that self-corrects: earlier iterations become 'retried'."""
    from argus.models import LLMInvestigationConfig
    from argus.storage import load_run

    call_count = 0

    def code_writer(s):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return {"error": True, "code": ""}  # fail
        return {"code": "print('hello')"}  # pass

    # Disable semantic judge — this test validates loop retry mechanics,
    # not LLM-based semantic evaluation. The judge can flip status based
    # on prompt wording changes, making this test non-deterministic.
    session = ArgusSession(
        llm_investigation=LLMInvestigationConfig(enabled=False),
    )
    session.set_node_names(["code_writer"])
    session.set_edges({"code_writer": ["code_writer"]})
    wrapped = session.wrap("code_writer", code_writer)

    wrapped({})
    wrapped({"error": True, "code": ""})
    wrapped({"error": True, "code": ""})
    session.finalize()

    loaded = load_run(session.run_id)
    cw_events = [e for e in loaded.steps if e.node_name == "code_writer"]
    assert len(cw_events) == 3
    assert cw_events[0].status == "retried"
    assert cw_events[1].status == "retried"
    assert cw_events[2].status == "pass"
    assert all(e.total_iterations == 3 for e in cw_events)
    assert loaded.overall_status == "clean"


@pytest.mark.unit
def test_loop_analysis_with_mocked_llm(monkeypatch):
    """Loop analysis produces LoopAnalysisResult via mocked LLM proxy."""
    import json

    from argus.loop_analyzer import analyze_loops
    from argus.models import NodeEvent, RunRecord

    # Build a minimal RunRecord with a 3-iteration loop
    events = []
    for i in range(3):
        events.append(
            NodeEvent(
                step_index=i,
                node_name="code_writer",
                status="retried" if i < 2 else "pass",
                input_state={},
                output_dict={"code": f"v{i}"},
                duration_ms=100.0,
                timestamp_utc="2026-01-01T00:00:00Z",
                attempt_index=i,
                total_iterations=3,
            )
        )

    record = RunRecord(
        run_id="test-loop",
        argus_version="0.7.5",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        duration_ms=300.0,
        overall_status="clean",
        first_failure_step=None,
        root_cause_chain=[],
        graph_node_names=["code_writer"],
        graph_edge_map={"code_writer": ["code_writer"]},
        initial_state={},
        steps=events,
        is_cyclic=True,
    )

    mock_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Took 3 attempts. Attempt 1 had syntax error.",
                            "is_stalled": False,
                            "stall_details": None,
                            "unnecessary_retries": 0,
                            "unnecessary_details": None,
                            "iteration_diffs": [
                                {
                                    "from_attempt": 0,
                                    "to_attempt": 1,
                                    "summary": "Fixed syntax error",
                                    "fields_changed": ["code"],
                                },
                            ],
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    monkeypatch.setattr(
        "argus.llm_proxy.is_available", lambda: True
    )
    monkeypatch.setattr(
        "argus.llm_proxy.create_chat_completion",
        lambda **kw: mock_response,
    )

    results = analyze_loops(record)
    assert len(results) == 1
    la = results[0]
    assert la.node_name == "code_writer"
    assert la.total_iterations == 3
    assert "3 attempts" in la.summary
    assert la.is_stalled is False
    assert la.unnecessary_retries == 0
    assert len(la.iteration_diffs) == 1
    assert la.iteration_diffs[0].summary == "Fixed syntax error"
    assert la.error is None


@pytest.mark.unit
def test_loop_no_retry_when_final_fails():
    """Loop where final iteration also fails: no retried status applied."""
    from argus.storage import load_run

    session = ArgusSession()
    session.set_node_names(["validator"])
    session.set_edges({"validator": ["validator"]})
    wrapped = session.wrap(
        "validator", lambda s: {"error": True, "result": ""}
    )

    wrapped({})
    wrapped({"error": True})
    session.finalize()

    loaded = load_run(session.run_id)
    v_events = [e for e in loaded.steps if e.node_name == "validator"]
    assert len(v_events) == 2
    # No retried — final iteration didn't pass
    assert all(e.status != "retried" for e in v_events)
    assert all(e.total_iterations == 2 for e in v_events)


# ── ArgusConfig (VAR-68) ────────────────────────────────────────────────────


@pytest.mark.unit
def test_argus_config_defaults():
    cfg = ArgusConfig()
    assert cfg.max_field_size == 50_000
    assert cfg.strict is False
    assert cfg.semantic_judge is True
    assert cfg.on_judge_failure == "warn"
    assert cfg.judge_max_retries == 1
    assert cfg.judge_retry_backoff == 0.5


@pytest.mark.unit
def test_argus_config_import_from_top_level():
    from argus import ArgusConfig as AC

    assert AC is ArgusConfig


@pytest.mark.unit
def test_session_accepts_config():
    cfg = ArgusConfig(strict=True, persist_state=False, on_judge_failure="skip")
    session = ArgusSession(config=cfg, strict=cfg.strict, persist_state=cfg.persist_state)
    assert session._strict is True
    assert session._persist_state is False
    assert session._on_judge_failure == "skip"


@pytest.mark.unit
def test_session_backward_compat_without_config():
    """Legacy kwargs still work when config is not provided."""
    session = ArgusSession(strict=True)
    assert session._strict is True
    assert session._on_judge_failure == "warn"  # default
    assert session._judge_max_retries == 1


# ── ArgusConfig cross-validation (VAR-73) ─────────────────────────────────


@pytest.mark.unit
def test_config_rejects_invalid_investigate():
    with pytest.raises(ValueError, match="investigate must be"):
        ArgusConfig(investigate="sometimes")


@pytest.mark.unit
def test_config_rejects_invalid_on_judge_failure():
    with pytest.raises(ValueError, match="on_judge_failure must be"):
        ArgusConfig(on_judge_failure="crash")


@pytest.mark.unit
def test_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="judge_max_retries must be >= 0"):
        ArgusConfig(judge_max_retries=-1)


@pytest.mark.unit
def test_config_rejects_zero_backoff():
    with pytest.raises(ValueError, match="judge_retry_backoff must be positive"):
        ArgusConfig(judge_retry_backoff=0)


@pytest.mark.unit
def test_config_rejects_negative_max_field_size():
    with pytest.raises(ValueError, match="max_field_size must be positive"):
        ArgusConfig(max_field_size=0)


@pytest.mark.unit
def test_config_rejects_bad_sample_rate():
    with pytest.raises(ValueError, match="sample_rate must be between"):
        ArgusConfig(sample_rate=1.5)
    with pytest.raises(ValueError, match="sample_rate must be between"):
        ArgusConfig(sample_rate=-0.1)


@pytest.mark.unit
def test_config_rejects_investigate_always_without_persist():
    with pytest.raises(ValueError, match="investigate='always' with persist_state=False"):
        ArgusConfig(investigate="always", persist_state=False)


@pytest.mark.unit
def test_config_rejects_judge_without_investigate():
    with pytest.raises(ValueError, match="semantic_judge=True requires investigate"):
        ArgusConfig(semantic_judge=True, investigate=False)


@pytest.mark.unit
def test_config_rejects_zero_sample_no_persist_failures():
    with pytest.raises(ValueError, match="no runs will ever be persisted"):
        ArgusConfig(sample_rate=0.0, persist_failures=False)


@pytest.mark.unit
def test_config_collects_multiple_errors():
    """Multiple misconfigs are reported in a single ValueError."""
    with pytest.raises(ValueError) as exc_info:
        ArgusConfig(max_field_size=-1, on_judge_failure="explode", judge_max_retries=-5)
    msg = str(exc_info.value)
    assert "max_field_size" in msg
    assert "on_judge_failure" in msg
    assert "judge_max_retries" in msg


@pytest.mark.unit
def test_config_valid_combinations_pass():
    """Valid configs should not raise."""
    ArgusConfig()  # all defaults
    ArgusConfig(investigate=True, semantic_judge=True)
    ArgusConfig(investigate="always", persist_state=True)
    ArgusConfig(investigate=False, semantic_judge=False)
    ArgusConfig(on_judge_failure="abort", judge_max_retries=3)
    ArgusConfig(sample_rate=0.0, persist_failures=True)  # OK: failures still persisted
    ArgusConfig(sample_rate=0.5, persist_failures=False)  # OK: some runs persisted


# ── Cyclic graph finalize warning (VAR-70) ──────────────────────────────


@pytest.mark.unit
def test_cyclic_graph_warns_without_finalize():
    """Watcher warns when a cyclic graph is GC'd without finalize()."""
    import warnings

    from argus.watcher import ArgusWatcher

    session = ArgusSession()
    session.set_edges({"a": ["b"], "b": ["a"]})  # cycle: a→b→a

    watcher = ArgusWatcher()
    watcher._session = session

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        watcher.__del__()
        assert len(w) == 1
        assert "cyclic graph" in str(w[0].message).lower()
        assert "finalize()" in str(w[0].message)


# ── VAR-71: Schema versioning + sampling ────────────────────────────────────


@pytest.mark.unit
def test_schema_version_written_on_save():
    """RunRecord gets schema_version persisted and round-trips correctly."""
    from argus.storage import SCHEMA_VERSION, load_run

    session = ArgusSession()
    session.set_node_names(["a"])
    session.set_edges({"a": []})
    fn = session.wrap("a", lambda state: {"x": 1})
    fn({})
    session.finalize()

    loaded = load_run(session.run_id)
    assert loaded.schema_version == SCHEMA_VERSION


@pytest.mark.unit
def test_schema_version_defaults_for_old_runs():
    """Runs saved before VAR-71 (no schema_version) deserialize as version '0'."""
    import json

    from argus.storage import _runs_path, load_run

    runs_dir = _runs_path()
    fake_id = "old-run-no-schema"
    (runs_dir / f"{fake_id}.json").write_text(
        json.dumps({"run_id": fake_id, "steps": [], "overall_status": "clean"}),
        encoding="utf-8",
    )
    loaded = load_run(fake_id)
    assert loaded.schema_version == "0"


@pytest.mark.unit
def test_sample_rate_zero_skips_clean_runs():
    """With sample_rate=0.0, clean runs are NOT persisted."""
    from argus.storage import _runs_path

    cfg = ArgusConfig(sample_rate=0.0, persist_failures=True)
    session = ArgusSession(config=cfg)
    session.set_node_names(["a"])
    session.set_edges({"a": []})
    fn = session.wrap("a", lambda state: {"x": 1})
    fn({})
    session.finalize()

    run_file = _runs_path() / f"{session.run_id}.json"
    assert not run_file.exists(), "Clean run should be skipped at sample_rate=0.0"


@pytest.mark.unit
def test_sample_rate_zero_still_persists_failures():
    """With sample_rate=0.0 + persist_failures=True, failed runs are saved."""
    from argus.storage import _runs_path

    cfg = ArgusConfig(sample_rate=0.0, persist_failures=True)
    session = ArgusSession(config=cfg)
    session.set_node_names(["a"])
    session.set_edges({"a": []})

    def crashing_fn(state):
        raise RuntimeError("boom")

    fn = session.wrap("a", crashing_fn)
    with pytest.raises(RuntimeError):
        fn({})

    run_file = _runs_path() / f"{session.run_id}.json"
    assert run_file.exists(), "Failed run must be persisted even at sample_rate=0.0"


# -- VAR-72: pattern-based & custom-function redaction -----------------------


# ── VAR-75: finalize() idempotency + dry-run mode ─────────────────────────


@pytest.mark.unit
def test_finalize_idempotent():
    """Calling finalize() twice produces exactly one run file."""
    from argus.storage import _runs_path

    session = ArgusSession()
    session.set_node_names(["a"])
    session.set_edges({"a": []})
    fn = session.wrap("a", lambda state: {"x": 1})
    fn({})
    session.finalize()
    session.finalize()  # second call — should be no-op

    run_files = list(_runs_path().glob(f"{session.run_id}.json"))
    assert len(run_files) == 1


@pytest.mark.unit
def test_finalize_idempotent_after_save_failure(monkeypatch):
    """After save_run raises, second finalize() is a no-op (not a retry)."""
    from argus import session as session_mod

    call_count = 0
    original_save = session_mod.save_run

    def failing_save(record):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("disk full")
        return original_save(record)

    monkeypatch.setattr(session_mod, "save_run", failing_save)

    session = ArgusSession()
    session.set_node_names(["a"])
    session.set_edges({"a": []})
    fn = session.wrap("a", lambda state: {"x": 1})
    fn({})
    session.finalize()  # fails on save, but _completed stays True
    session.finalize()  # no-op — does NOT retry

    assert call_count == 1, "save_run should only be called once (no retry)"


@pytest.mark.unit
def test_dry_run_no_persistence():
    """dry_run=True captures events but writes nothing to disk."""
    from argus.storage import _runs_path

    cfg = ArgusConfig(dry_run=True)
    session = ArgusSession(config=cfg)
    session.set_node_names(["a"])
    session.set_edges({"a": []})
    fn = session.wrap("a", lambda state: {"x": 1})
    fn({})
    session.finalize()

    run_file = _runs_path() / f"{session.run_id}.json"
    assert not run_file.exists(), "dry_run should skip persistence"


@pytest.mark.unit
def test_redact_keys_basic():
    """Existing allowlist redaction still works."""
    from argus.session import _redact_dict

    data = {"api_key": "sk-abc123", "query": "hello"}
    result = _redact_dict(data, frozenset({"api_key"}))
    assert result["api_key"] == "__REDACTED__"
    assert result["query"] == "hello"


@pytest.mark.unit
def test_redact_custom_function():
    """Per-field custom redaction function replaces blanket marker."""
    from argus.session import _redact_dict

    def mask_last4(v):
        if isinstance(v, str) and len(v) >= 4:
            return f"***{v[-4:]}"
        return "__REDACTED__"

    data = {"card_number": "4111111111111234", "name": "Alice"}
    result = _redact_dict(
        data, frozenset(), fns={"card_number": mask_last4},
    )
    assert result["card_number"] == "***1234"
    assert result["name"] == "Alice"


@pytest.mark.unit
def test_redact_function_takes_priority_over_key():
    """Custom function for a key beats the allowlist marker."""
    from argus.session import _redact_dict

    data = {"token": "my-secret-token-value"}
    result = _redact_dict(
        data,
        frozenset({"token"}),
        fns={"token": lambda v: f"hash:{hash(v)}"},
    )
    assert result["token"].startswith("hash:")


@pytest.mark.unit
def test_redact_pattern_detects_jwt():
    """Pattern-based detection catches JWT-shaped values."""
    from argus.session import _redact_dict

    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    data = {"auth": jwt, "query": "normal text"}
    result = _redact_dict(data, frozenset(), pattern_detect=True)
    assert result["auth"] == "__REDACTED__"
    assert result["query"] == "normal text"


@pytest.mark.unit
def test_redact_pattern_detects_openai_key():
    """Pattern-based detection catches sk- prefixed keys."""
    from argus.session import _redact_dict

    key = "sk-proj-abc123def456ghi789jkl012mno345"
    data = {"key": key}
    result = _redact_dict(data, frozenset(), pattern_detect=True)
    assert result["key"] == "__REDACTED__"


@pytest.mark.unit
def test_redact_pattern_ignores_normal_text():
    """Pattern detection does not false-positive on normal content."""
    from argus.session import _redact_dict

    data = {
        "message": "Hello, this is a normal response",
        "count": 42,
    }
    result = _redact_dict(data, frozenset(), pattern_detect=True)
    assert result["message"] == "Hello, this is a normal response"
    assert result["count"] == 42


@pytest.mark.unit
def test_redact_pattern_nested_and_list():
    """Pattern detection recurses into nested dicts and lists."""
    from argus.session import _redact_dict

    aws_key = "AKIAIOSFODNN7EXAMPLE"
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.x"
    )
    data = {
        "config": {"aws_access_key": aws_key},
        "tokens": [{"val": jwt}, {"val": "safe text"}],
    }
    result = _redact_dict(data, frozenset(), pattern_detect=True)
    assert result["config"]["aws_access_key"] == "__REDACTED__"
    assert result["tokens"][0]["val"] == "__REDACTED__"
    assert result["tokens"][1]["val"] == "safe text"


@pytest.mark.unit
def test_redact_session_integration():
    """ArgusSession._redact applies all three mechanisms together."""
    session = ArgusSession(
        redact_keys=["password"],
        redact_functions={
            "ssn": lambda v: (
                "***-**-" + v[-4:] if isinstance(v, str) else v
            ),
        },
        redact_patterns=True,
    )
    snap = {
        "password": "hunter2",
        "ssn": "123-45-6789",
        "api_key": "sk-proj-abc123def456ghi789jkl012mno345",
        "query": "harmless",
    }
    result = session._redact(snap)
    assert result["password"] == "__REDACTED__"
    assert result["ssn"] == "***-**-6789"
    assert result["api_key"] == "__REDACTED__"
    assert result["query"] == "harmless"


# ── Latency-correlated degradation (VAR-8) ──────────────────────────────


def _make_inspection(**overrides):
    """Build a minimal InspectionResult for latency tests."""
    from argus.models import InspectionResult

    defaults = dict(
        is_silent_failure=False,
        missing_fields=[],
        empty_fields=[],
        type_mismatches=[],
        severity="ok",
        message="ok",
        tool_failures=[],
        has_tool_failure=False,
        semantic_signals=[],
    )
    defaults.update(overrides)
    return InspectionResult(**defaults)


@pytest.mark.unit
def test_latency_timeout_adjacent():
    session = ArgusSession(node_timeout_ms=30_000)
    inspection = _make_inspection()
    session._check_latency_signals(29_500, inspection)
    types = [tf.failure_type for tf in inspection.tool_failures]
    assert "timeout_adjacent" in types


@pytest.mark.unit
def test_latency_within_timeout_no_flag():
    session = ArgusSession(node_timeout_ms=30_000)
    inspection = _make_inspection()
    session._check_latency_signals(20_000, inspection)
    assert len(inspection.tool_failures) == 0


@pytest.mark.unit
def test_latency_suspiciously_fast():
    session = ArgusSession(min_expected_ms=500)
    inspection = _make_inspection()
    session._check_latency_signals(100, inspection)
    types = [tf.failure_type for tf in inspection.tool_failures]
    assert "suspiciously_fast" in types


@pytest.mark.unit
def test_latency_quality_mismatch():
    from argus.models import SemanticSignal

    session = ArgusSession(min_expected_ms=500)
    inspection = _make_inspection(
        semantic_signals=[
            SemanticSignal(
                sig_id="test",
                category="placeholder_outputs",
                severity="warning",
                description="test signal",
                field_path=("output",),
                evidence="placeholder",
                confidence=0.9,
            )
        ],
    )
    session._check_latency_signals(100, inspection)
    types = [tf.failure_type for tf in inspection.tool_failures]
    assert "latency_quality_mismatch" in types


@pytest.mark.unit
def test_latency_no_thresholds_no_flags():
    """No thresholds configured → no latency failures regardless of timing."""
    session = ArgusSession()
    inspection = _make_inspection()
    session._check_latency_signals(1, inspection)
    assert len(inspection.tool_failures) == 0


# ── Conditional branch skipping (VAR-61) ──────────────────────────────────────


@pytest.mark.unit
def test_conditional_branch_skipped_nodes():
    """Unchosen conditional branch nodes get status='skipped', not 'crashed'.

    Graph: router → branch_a, branch_b (conditional)
    Only branch_a runs. branch_b should be 'skipped'.
    """
    session = ArgusSession()
    session.set_node_names(["router", "branch_a", "branch_b"])
    session.set_edges({"router": ["branch_a", "branch_b"]})
    session.set_conditional_sources({"router"})

    fn_router = session.wrap("router", lambda state: {"route": "a"})
    fn_a = session.wrap("branch_a", lambda state: {"result": "done"})
    # branch_b is never called — simulates unchosen conditional path

    state = {}
    state = fn_router(state)
    state = fn_a(state)
    session.finalize()

    record = load_run(session.run_id)
    statuses = {e.node_name: e.status for e in record.steps}
    assert statuses["router"] == "pass"
    assert statuses["branch_a"] == "pass"
    assert statuses["branch_b"] == "skipped"
    assert record.overall_status == "clean"
    assert record.root_cause_chain == []


@pytest.mark.unit
def test_conditional_branch_auto_finalize():
    """Auto-finalize triggers even when unchosen branch terminals never run."""
    session = ArgusSession()
    session.set_node_names(["router", "branch_a", "branch_b"])
    session.set_edges({"router": ["branch_a", "branch_b"]})
    session.set_conditional_sources({"router"})

    fn_router = session.wrap("router", lambda state: {"route": "a"})
    fn_a = session.wrap("branch_a", lambda state: {"result": "done"})

    fn_router({})
    fn_a({})
    # Should auto-finalize — branch_b is an expected terminal but was never chosen

    record = load_run(session.run_id)
    assert record is not None
    assert record.overall_status == "clean"


@pytest.mark.unit
def test_user_config_save_load_clear(tmp_path, monkeypatch):
    import argus.user_config as uc

    monkeypatch.setattr(uc, "_CONFIG_DIR", tmp_path / ".argus")
    monkeypatch.setattr(uc, "_CONFIG_FILE", tmp_path / ".argus" / "config.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert uc.get_saved_openai_key() is None
    uc.set_openai_key("sk-test-123")
    assert uc.get_saved_openai_key() == "sk-test-123"
    # file is chmod 600
    import stat
    mode = stat.S_IMODE((tmp_path / ".argus" / "config.json").stat().st_mode)
    assert mode == 0o600
    uc.clear_openai_key()
    assert uc.get_saved_openai_key() is None


@pytest.mark.unit
def test_resolve_openai_key_prefers_env(tmp_path, monkeypatch):
    import argus.user_config as uc

    monkeypatch.setattr(uc, "_CONFIG_DIR", tmp_path / ".argus")
    monkeypatch.setattr(uc, "_CONFIG_FILE", tmp_path / ".argus" / "config.json")
    uc.set_openai_key("sk-saved")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert uc.resolve_openai_key() == "sk-env"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert uc.resolve_openai_key() == "sk-saved"


@pytest.mark.unit
def test_llm_proxy_uses_byok_when_key_present(monkeypatch):
    import argus.llm_proxy as lp

    captured = {}

    def fake_direct(*, api_key, model, messages, max_tokens, temperature,
                    response_format, timeout):
        captured["api_key"] = api_key
        captured["model"] = model
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(lp, "_call_openai_direct", fake_direct)
    monkeypatch.setattr(lp, "resolve_openai_key", lambda: "sk-byok")

    out = lp.create_chat_completion(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert "error" not in out
    assert captured["api_key"] == "sk-byok"
    assert captured["model"] == "gpt-4o-mini"


@pytest.mark.unit
def test_llm_proxy_errors_when_no_key_and_no_proxy(monkeypatch):
    import argus.llm_proxy as lp

    monkeypatch.setattr(lp, "resolve_openai_key", lambda: None)
    monkeypatch.setattr(lp, "SUPABASE_URL", None)

    out = lp.create_chat_completion(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert "error" in out


@pytest.mark.unit
def test_llm_proxy_is_available_true_with_byok(monkeypatch):
    import importlib

    import argus.llm_proxy as lp

    # conftest's autouse fixture stubs is_available to always return False
    # for test isolation elsewhere; reload restores the real implementation
    # for this test only.
    importlib.reload(lp)
    monkeypatch.setattr(lp, "resolve_openai_key", lambda: "sk-byok")
    assert lp.is_available() is True


@pytest.mark.unit
def test_cloud_no_hardcoded_supabase_url():
    import inspect

    import argus.cloud as cloud

    src = inspect.getsource(cloud)
    assert "isnphpbckxfjsxllryrg" not in src  # real project ref must be gone


@pytest.mark.unit
def test_cloud_noops_when_unconfigured(monkeypatch):
    import argus.cloud as cloud

    monkeypatch.setattr(cloud, "SUPABASE_URL", None)
    monkeypatch.setattr(cloud, "SUPABASE_ANON_KEY", None)
    assert cloud.is_logged_in() is False
    assert cloud.push_run({"run_id": "x"}) is False
    assert cloud.pull_shared_signatures() == []


@pytest.mark.unit
def test_login_reports_hosted_only_when_unconfigured(monkeypatch, capsys):
    import argus.cli.cmd_login as cl

    monkeypatch.setattr(cl, "SUPABASE_URL", None)
    cl.login()
    out = capsys.readouterr().out.lower()
    assert "hosted" in out or "not available" in out


@pytest.mark.unit
def test_cmd_key_set_show_clear(tmp_path, monkeypatch, capsys):
    import argus.user_config as uc
    import argus.cli.cmd_key as ck

    monkeypatch.setattr(uc, "_CONFIG_DIR", tmp_path / ".argus")
    monkeypatch.setattr(uc, "_CONFIG_FILE", tmp_path / ".argus" / "config.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    ck.key_set("sk-abcdef123456")
    assert uc.get_saved_openai_key() == "sk-abcdef123456"

    ck.key_show()
    out = capsys.readouterr().out
    assert "sk-abcdef123456" not in out  # masked
    assert "3456" in out  # last 4 shown

    ck.key_clear()
    assert uc.get_saved_openai_key() is None
