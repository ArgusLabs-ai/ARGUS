"""ArgusSession — framework-agnostic monitoring session.

Works with any Python pipeline: LangGraph, Prefect, Temporal, raw functions, etc.
ArgusWatcher is a thin LangGraph adapter that builds an ArgusSession internally.

Usage without LangGraph:
    from argus import ArgusSession

    session = ArgusSession(validators={
        "validate": lambda out: (out.get("score", 0) > 0.5, "Score too low"),
        "*": lambda out: ("error" not in out, f"Node error: {out.get('error')}"),
    })
    session.set_edges({"fetch": ["validate"], "validate": ["process"]})

    fetch   = session.wrap("fetch",    fetch_fn)
    validate = session.wrap("validate", validate_fn)
    process  = session.wrap("process",  process_fn)

    state = fetch(initial_state)
    state = validate(state)
    state = process(state)
    session.finalize()
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import json
import threading
import time
import traceback
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from argus import __version__
from argus.anomaly_detector import detect_anomalies
from argus.inspector import (
    build_root_cause_chain,
    inspect_transition,
    is_legitimate_field_handoff,
)
from argus.llm_tracker import create_tracker, extract_usage, install_handler, remove_handler
from argus.models import (
    AnomalySignal,
    ArgusConfig,
    BehaviorConfig,
    DisambiguationResult,
    InspectionResult,
    LLMInvestigationConfig,
    LLMUsage,
    NodeEvent,
    RunRecord,
    RunStatus,
    SemanticCheckResult,
    SemanticSignal,
    StepStatus,
    ToolFailure,
    ValidatorResult,
)
from argus.storage import save_run
from argus.utils.cycle_detection import has_cycles
from argus.utils.ids import generate_run_id
from argus.utils.serializer import safe_serialize


@dataclass
class _PendingJudge:
    """Tracks a background LLM semantic check for deferred application."""

    event: NodeEvent
    future: Future
    inspection: InspectionResult | None
    validator_results: list[ValidatorResult]
    anomaly_signals: list[AnomalySignal]
    ambiguous_signals: list[SemanticSignal]
    deterministic_status: StepStatus
    node_name: str
    input_snap: dict
    output_snap: dict


# Optional GraphInterrupt import — only available when langgraph is installed
try:
    from langgraph.errors import GraphInterrupt as _GraphInterrupt  # type: ignore[import]
except ImportError:
    _GraphInterrupt = None  # type: ignore[assignment,misc]

# Sentinel for _pop_frozen_output — distinct from any real output value
_MISSING = object()

_REDACTED = "__REDACTED__"

# Built-in patterns that match common secret shapes (compiled once at import)
import re as _re  # noqa: E402

_SECRET_PATTERNS: list[_re.Pattern[str]] = [
    _re.compile(r"^eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # JWT
    _re.compile(r"^sk-[A-Za-z0-9\-_]{20,}"),  # OpenAI / Stripe secret keys
    _re.compile(r"^AKIA[0-9A-Z]{16}$"),  # AWS access key ID
    _re.compile(r"^ghp_[A-Za-z0-9]{36,}$"),  # GitHub PAT
    _re.compile(r"^glpat-[A-Za-z0-9\-_]{20,}$"),  # GitLab PAT
    _re.compile(r"^xox[bpras]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
    _re.compile(r"^Bearer\s+[A-Za-z0-9\-._~+/]+=*$"),  # Bearer tokens
    _re.compile(r"^[A-Za-z0-9+/]{40,}={0,2}$"),  # long base64 blobs (>=40 chars)
]


def _looks_like_secret(value: Any) -> bool:
    """Return True if a string value matches common secret token shapes."""
    if not isinstance(value, str) or len(value) < 20:
        return False
    return any(p.search(value) for p in _SECRET_PATTERNS)


def _redact_dict(
    d: dict[str, Any],
    keys: frozenset[str],
    fns: dict[str, Callable[[Any], Any]] | None = None,
    pattern_detect: bool = False,
) -> dict[str, Any]:
    """Recursively replace values of sensitive keys with a redaction marker.

    *fns* maps field names to custom redaction callables (e.g. hash, mask-last-4).
    *pattern_detect* enables heuristic detection of secret-shaped string values.
    """
    out: dict[str, Any] = {}
    for k, v in d.items():
        if fns and k in fns:
            out[k] = fns[k](v)
        elif k in keys:
            out[k] = _REDACTED
        elif pattern_detect and _looks_like_secret(v):
            out[k] = _REDACTED
        elif isinstance(v, dict):
            out[k] = _redact_dict(v, keys, fns, pattern_detect)
        elif isinstance(v, list):
            out[k] = [
                _redact_dict(item, keys, fns, pattern_detect)
                if isinstance(item, dict)
                else (_REDACTED if pattern_detect and _looks_like_secret(item) else item)
                for item in v
            ]
        else:
            out[k] = v
    return out


def _is_empty_value(value: Any) -> bool:
    """Check if a value is semantically empty (None, empty string, empty collection)."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, tuple, dict, set)) and len(value) == 0:
        return True
    return False


def _measure_output_depth(obj: Any, current: int = 0) -> int:
    """Max nesting depth of a dict/list (privacy-safe shape metric)."""
    if isinstance(obj, dict):
        if not obj:
            return current + 1
        return max(_measure_output_depth(v, current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return current + 1
        return max(_measure_output_depth(item, current + 1) for item in obj)
    return current


def _merge_candidate(
    data: dict[str, Any],
    cluster_id: str,
    sig: Any,
    run_id: str,
) -> None:
    """Merge evidence from a new signature into an existing candidate."""
    from datetime import datetime, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat()
    for cand in data.get("candidates", []):
        if cand["id"] == cluster_id:
            cand["times_seen"] = cand.get("times_seen", 1) + 1
            cand["last_seen"] = now
            for ev in sig.evidence:
                if ev not in cand.get("evidence", []):
                    cand.setdefault("evidence", []).append(ev)
            if run_id and run_id not in cand.get("source_run_ids", []):
                cand.setdefault("source_run_ids", []).append(run_id)
            break


def _compute_coverage_summary(
    events: list[Any],
    graph_node_names: list[str],
) -> dict[str, float]:
    """Fraction of the run each detection layer actually evaluated, 0.0–1.0.

    Distinguishes "checked, clean" from "never checked" (VAR-103). A layer is
    omitted from the result when it attempted nothing (e.g. the judge never ran
    on any node), rather than reported as 0% or a misleading 100%.
    """
    from argus.registry import heuristic_coverage  # noqa: PLC0415

    summary: dict[str, float] = {}

    # Structural: nodes whose transition could not be checked because every
    # successor they feed lacked a field-typed schema.
    # ponytail: node-level approximation — a node with *some* annotated
    # successors still counts as covered. Refining needs InspectionResult to
    # expose annotated_count (VAR-104 expensive half), out of scope here.
    node_set = set(graph_node_names)
    total_nodes = len(node_set) or len({e.node_name for e in events})
    if total_nodes:
        unannotated: set[str] = set()
        for e in events:
            insp = getattr(e, "inspection", None)
            if insp is not None and insp.unannotated_successors:
                unannotated.update(insp.unannotated_successors)
        blind = unannotated & node_set if node_set else unannotated
        summary["structural"] = round(1.0 - len(blind) / total_nodes, 4)

    # Heuristic: fraction of registry signatures actually usable this run.
    summary["heuristic"] = heuristic_coverage()

    # Judge: fraction of judged nodes that were actually evaluated (vs skipped
    # on error/timeout/not-logged-in). Only reported if the judge ran at all.
    judged = [e for e in events if getattr(e, "semantic_check", None) is not None]
    if judged:
        evaluated = sum(1 for e in judged if e.semantic_check.evaluated)
        summary["judge"] = round(evaluated / len(judged), 4)

    return summary



def _parse_validator_return(raw: Any) -> tuple[bool, str, str]:
    """Normalize validator returns: (ok, message) or (ok, message, severity)."""
    if not isinstance(raw, (tuple, list)) or len(raw) < 2:
        return False, f"Validator returned invalid result: {raw!r}", "critical"
    is_valid = bool(raw[0])
    message = str(raw[1])
    severity = "ok" if is_valid else "critical"
    if not is_valid and len(raw) >= 3 and raw[2] is not None:
        token = str(raw[2]).strip().lower()
        if token in ("warning", "warn"):
            severity = "warning"
        elif token in ("critical", "blocking", "error", "fail"):
            severity = "critical"
    return is_valid, message, severity


class ArgusSession:
    """Framework-agnostic monitoring session.

    Captures state, validates transitions, and saves a RunRecord on finalize().
    Can be used standalone (via wrap()) or driven by ArgusWatcher (via LangGraph).
    """

    def __init__(
        self,
        run_id: str | None = None,
        max_field_size: int = 50_000,
        validators: dict[str, Callable[[dict], tuple[bool, str]]] | None = None,
        parent_run_id: str | None = None,
        strict: bool = False,
        behavior_type: str | None = None,
        node_behaviors: dict[str, str] | None = None,
        llm_investigation: LLMInvestigationConfig | None = None,
        redact_keys: set[str] | list[str] | None = None,
        redact_functions: dict[str, Callable[[Any], Any]] | None = None,
        redact_patterns: bool = False,
        persist_state: bool = True,
        config: ArgusConfig | None = None,
        node_timeout_ms: float | None = None,
        min_expected_ms: float | None = None,
    ) -> None:
        self.run_id: str = run_id or generate_run_id()
        self.max_field_size = max_field_size
        self._node_timeout_ms = node_timeout_ms or (config.node_timeout_ms if config else None)
        self._min_expected_ms = min_expected_ms or (config.min_expected_ms if config else None)

        # Judge failure policy from typed config (defaults for backward compat)
        self._on_judge_failure = config.on_judge_failure if config else "warn"
        self._judge_max_retries = config.judge_max_retries if config else 1
        self._judge_retry_backoff = config.judge_retry_backoff if config else 0.5
        self._sample_rate = config.sample_rate if config else 1.0
        self._persist_failures = config.persist_failures if config else True
        self._dry_run = config.dry_run if config else False
        self.graph_node_names: list[str] = []
        self.graph_edge_map: dict[str, list[str]] = {}
        self.node_fn_registry: dict[str, Any] = {}

        self._strict = strict
        self._redact_keys: frozenset[str] = frozenset(redact_keys or ())
        self._redact_functions: dict[str, Callable[[Any], Any]] = redact_functions or {}
        self._redact_patterns: bool = config.redact_patterns if config else redact_patterns
        self._persist_state = persist_state

        # Behavior anomaly detection config
        self._behavior_config: BehaviorConfig | None = None
        if behavior_type or node_behaviors:
            self._behavior_config = BehaviorConfig(
                default_behavior_type=behavior_type,
                node_behaviors=node_behaviors or {},
            )

        # LLM semantic investigator config — auto-enable if user is logged in
        if llm_investigation is None:
            try:
                from dotenv import load_dotenv

                load_dotenv(override=True)
            except ImportError:
                pass
            from argus.llm_proxy import is_available as _llm_available

            if _llm_available():
                from argus.models import LLMInvestigationConfig

                llm_investigation = LLMInvestigationConfig(enabled=True)
        self._llm_investigation_config = llm_investigation

        # validator map: key is node name or "*" (wildcard)
        self._validators: dict[str, Callable[[dict], tuple[bool, str]]] = validators or {}

        self._lock = threading.Lock()
        self._events: list[NodeEvent] = []
        self._step_index = 0
        self._initial_state: dict[str, Any] = {}
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._completed = False
        # True while nested inside batch/abatch (or stream→invoke): last-node
        # auto-finalize must wait so items 2..N are not dropped after _completed.
        self._defer_auto_finalize = False
        self._is_cyclic = False
        self._conditional_sources: set[str] = set()
        self._has_conditional_edges: bool = False
        self._node_attempt_counts: dict[str, int] = {}

        # Async LLM judge: fire semantic checks in background threads,
        # collect results before _finalize. Cuts per-node blocking from
        # ~300ms to ~0ms; total LLM time overlaps across nodes.
        self._judge_pool: ThreadPoolExecutor | None = None
        self._pending_judges: list[_PendingJudge] = []
        self._pending_judges_lock = threading.Lock()

        # Sync shared community signatures from Supabase in the background.
        # Non-blocking — if not logged in or network fails, silently skips.
        threading.Thread(target=self._sync_shared_signatures, daemon=True).start()
        self._terminal_nodes: set[str] = set()
        self._completed_terminals: set[str] = set()

        # set by ReplayEngine or ArgusWatcher for linked runs
        self.parent_run_id: str | None = parent_run_id
        self.replay_from_step: str | None = None
        # patch applied to the replayed input state, recorded for provenance
        self.state_patch: dict[str, Any] | None = None

        # frozen outputs for replay — maps node_name → list of saved output dicts (FIFO)
        self.frozen_outputs: dict[str, list[Any]] | None = None

        # auto-captured for zero-config replay (set by ArgusWatcher)
        self.app_factory_ref: str | None = None
        self.node_fn_refs: dict[str, str] | None = None
        self.node_fn_paths: dict[str, str] | None = None

        # Reducer functions extracted from StateGraph schema (set by ArgusWatcher).
        # Maps field_name → reducer callable (e.g. operator.add).
        self.reducer_fields: dict[str, Any] = {}

        # Safety net: finalize on interpreter exit if the user forgot to call it.
        atexit.register(self._atexit_finalize)

    def _atexit_finalize(self) -> None:
        """Safety net: persist at interpreter exit if invoke() never completed."""
        if self._completed:
            return
        try:
            self._finalize()
        except Exception:
            pass

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _sync_shared_signatures() -> None:
        """Pull shared community signatures from Supabase and reload the registry.

        Runs in a daemon thread — safe to fail silently if not logged in or
        no network is available.
        """
        try:
            from argus.registry import sync_shared_signatures  # noqa: PLC0415

            sync_shared_signatures()
        except Exception:
            pass

    # ── Public configuration ─────────────────────────────────────────────────

    def set_edges(self, edge_map: dict[str, list[str]]) -> None:
        """Register the pipeline topology. Enables cycle detection and successor validation."""
        self.graph_edge_map = edge_map
        self._is_cyclic = has_cycles(edge_map)
        if self.graph_node_names:
            self._terminal_nodes = self._compute_terminal_nodes()

    def set_node_names(self, names: list[str]) -> None:
        """Register ordered node names. Used for last-node auto-finalization."""
        self.graph_node_names = names
        self._terminal_nodes = self._compute_terminal_nodes()

    def _compute_terminal_nodes(self) -> set[str]:
        """Find nodes with no outgoing edges to other graph nodes (DAG leaves).

        Terminal nodes are the "real" last nodes in the topology — auto-finalize
        triggers only after ALL terminal nodes have completed. This correctly
        handles parallel fan-out where multiple branches finish independently.
        """
        if not self.graph_node_names:
            return set()
        node_set = set(self.graph_node_names)
        terminals = set()
        for name in self.graph_node_names:
            successors = self.graph_edge_map.get(name, [])
            real_successors = [s for s in successors if s in node_set]
            if not real_successors:
                terminals.add(name)
        return terminals or {self.graph_node_names[-1]}

    def set_conditional_sources(self, sources: set[str]) -> None:
        """Register nodes that have conditional outgoing edges (routers)."""
        self._conditional_sources = sources
        self._has_conditional_edges = bool(sources)

    def _expected_terminals(self) -> set[str]:
        """Terminals expected to complete given current execution.

        For conditional graphs, only terminals that have actually executed
        are expected — unchosen branches don't block finalization.
        """
        if not self._has_conditional_edges:
            return self._terminal_nodes
        executed = {e.node_name for e in self._events}
        expected = {t for t in self._terminal_nodes if t in executed}
        # ponytail: fallback to all if none matched yet (early in execution)
        return expected or self._terminal_nodes

    # ── Function wrapping (framework-agnostic entry point) ───────────────────

    def wrap(self, node_name: str, fn: Callable) -> Callable:
        """Return a monitored version of fn. Works with sync and async functions."""
        if asyncio.iscoroutinefunction(fn):
            return self._make_async_wrapper(node_name, fn)
        return self._make_sync_wrapper(node_name, fn)

    def instrument(
        self,
        agents: dict[str, Callable],
        edges: dict[str, list[str]] | None = None,
    ) -> dict[str, Callable]:
        """Wrap all agents at once. Returns a dict of the same keys with monitored functions.

        Args:
            agents: mapping of {node_name: function} for every agent in your pipeline.
            edges:  optional topology — same as calling set_edges() separately.

        Example (15 agents):
            wrapped = session.instrument(
                agents={
                    "fetch":    fetch_fn,
                    "validate": validate_fn,
                    "process":  process_fn,
                    # ... all 15
                },
                edges={
                    "fetch":    ["validate"],
                    "validate": ["process"],
                    # ...
                },
            )
            state = wrapped["fetch"](state)
            state = wrapped["validate"](state)
        """
        if edges is not None:
            self.set_edges(edges)
        # register node order from insertion order (Python 3.7+)
        self.set_node_names(list(agents.keys()))
        # populate node_fn_registry with original functions so that
        # inspect_transition can read successor type annotations
        self.node_fn_registry.update(agents)
        return {name: self.wrap(name, fn) for name, fn in agents.items()}

    def node(self, node_name: str) -> Callable:
        """Decorator — instruments the function at definition time.

        Example:
            session = ArgusSession()

            @session.node("fetch")
            def fetch(state):
                ...

            @session.node("validate")
            def validate(state):
                ...
        """

        def decorator(fn: Callable) -> Callable:
            wrapped = self.wrap(node_name, fn)
            # Register name so node list stays up to date
            if node_name not in self.graph_node_names:
                self.graph_node_names.append(node_name)
            return wrapped

        return decorator

    def _pop_frozen_output(self, node_name: str) -> Any:
        """Pop the next frozen output for node_name, if available. Thread-safe."""
        with self._lock:
            frozen = self.frozen_outputs
            if frozen and node_name in frozen and frozen[node_name]:
                return frozen[node_name].pop(0)
        return _MISSING

    def _make_sync_wrapper(self, node_name: str, original_fn: Callable) -> Callable:
        @functools.wraps(original_fn)
        def _wrapped(state: Any, **kwargs: Any) -> Any:
            input_snap = self.capture_state(state)
            self.on_node_start(node_name, input_snap)
            tracker = create_tracker()
            handler_token = install_handler(tracker) if tracker else None
            t0 = time.perf_counter()
            try:
                frozen_out = self._pop_frozen_output(node_name)
                if frozen_out is not _MISSING:
                    output = frozen_out
                else:
                    output = original_fn(state, **kwargs)
                duration = (time.perf_counter() - t0) * 1000
                if tracker:
                    remove_handler(tracker, handler_token)
                output_snap = self.capture_output(output)
                llm_usage = extract_usage(tracker, output_snap)
                self.on_node_end(
                    node_name,
                    input_snap,
                    output_snap,
                    duration,
                    exc=None,
                    llm_usage=llm_usage,
                )
                return output
            except Exception as exc:
                duration = (time.perf_counter() - t0) * 1000
                if tracker:
                    remove_handler(tracker, handler_token)
                llm_usage = extract_usage(tracker, None)
                # Detect GraphInterrupt before treating as crash
                if _GraphInterrupt is not None and isinstance(exc, _GraphInterrupt):
                    self.on_node_end(
                        node_name,
                        input_snap,
                        None,
                        duration,
                        exc=None,
                        is_interrupt=True,
                        llm_usage=llm_usage,
                    )
                    raise
                self.on_node_end(
                    node_name,
                    input_snap,
                    None,
                    duration,
                    exc=exc,
                    llm_usage=llm_usage,
                )
                raise

        return _wrapped

    def _make_async_wrapper(self, node_name: str, original_fn: Callable) -> Callable:
        @functools.wraps(original_fn)
        async def _wrapped(state: Any, **kwargs: Any) -> Any:
            input_snap = self.capture_state(state)
            self.on_node_start(node_name, input_snap)
            tracker = create_tracker()
            handler_token = install_handler(tracker) if tracker else None
            t0 = time.perf_counter()
            try:
                frozen_out = self._pop_frozen_output(node_name)
                if frozen_out is not _MISSING:
                    output = frozen_out
                else:
                    output = await original_fn(state, **kwargs)
                duration = (time.perf_counter() - t0) * 1000
                if tracker:
                    remove_handler(tracker, handler_token)
                output_snap = self.capture_output(output)
                llm_usage = extract_usage(tracker, output_snap)
                self.on_node_end(
                    node_name,
                    input_snap,
                    output_snap,
                    duration,
                    exc=None,
                    llm_usage=llm_usage,
                )
                return output
            except Exception as exc:
                duration = (time.perf_counter() - t0) * 1000
                if tracker:
                    remove_handler(tracker, handler_token)
                llm_usage = extract_usage(tracker, None)
                if _GraphInterrupt is not None and isinstance(exc, _GraphInterrupt):
                    self.on_node_end(
                        node_name,
                        input_snap,
                        None,
                        duration,
                        exc=None,
                        is_interrupt=True,
                        llm_usage=llm_usage,
                    )
                    raise
                self.on_node_end(
                    node_name,
                    input_snap,
                    None,
                    duration,
                    exc=exc,
                    llm_usage=llm_usage,
                )
                raise

        return _wrapped

    # ── State capture ─────────────────────────────────────────────────────────

    def _redact(self, snap: dict[str, Any]) -> dict[str, Any]:
        """Replace values of sensitive keys with a redaction marker.

        Recurses into nested dicts and list items. Only modifies the
        serialized snapshot — the original state passed to the node is
        never touched.
        """
        has_work = self._redact_keys or self._redact_functions or self._redact_patterns
        if not has_work:
            return snap
        return _redact_dict(
            snap, self._redact_keys, self._redact_functions or None, self._redact_patterns
        )

    def capture_state(self, state: Any) -> dict[str, Any]:
        snap = safe_serialize(state, self.max_field_size)
        if not self._initial_state and snap:
            with self._lock:
                if not self._initial_state:
                    self._initial_state = self._redact(snap)
        return snap

    def capture_output(self, output: Any) -> dict[str, Any]:
        return safe_serialize(output, self.max_field_size)

    # ── Latency-correlated degradation ──────────────────────────────────────

    def _check_latency_signals(
        self, duration_ms: float, inspection: InspectionResult
    ) -> None:
        """Append latency-based ToolFailure entries to an existing inspection."""
        # 1. Timeout-adjacent — output likely truncated
        if self._node_timeout_ms and duration_ms / self._node_timeout_ms > 0.95:
            inspection.tool_failures.append(
                ToolFailure(
                    failure_type="timeout_adjacent",
                    field_name="_latency",
                    severity="warning",
                    evidence=(
                        f"{duration_ms:.0f}ms is >=95% of "
                        f"{self._node_timeout_ms:.0f}ms timeout"
                    ),
                )
            )
        # 2. Suspiciously fast — likely cached/stale
        if self._min_expected_ms and duration_ms < self._min_expected_ms:
            inspection.tool_failures.append(
                ToolFailure(
                    failure_type="suspiciously_fast",
                    field_name="_latency",
                    severity="warning",
                    evidence=(
                        f"{duration_ms:.0f}ms < expected minimum "
                        f"{self._min_expected_ms:.0f}ms"
                    ),
                )
            )
        # 3. Fast + already-failed = cached failure
        fast_threshold = self._min_expected_ms or 500.0
        has_existing_failure = (
            inspection.is_silent_failure
            or inspection.has_tool_failure
            or bool(inspection.semantic_signals)
        )
        if duration_ms < fast_threshold and has_existing_failure:
            inspection.tool_failures.append(
                ToolFailure(
                    failure_type="latency_quality_mismatch",
                    field_name="_latency",
                    severity="warning",
                    evidence=(
                        f"Completed in {duration_ms:.0f}ms with quality issues "
                        f"— likely cached/stale failure"
                    ),
                )
            )

    # ── Event recording ───────────────────────────────────────────────────────

    def on_node_start(self, node_name: str, input_snap: dict[str, Any]) -> None:
        pass  # reserved for future streaming / real-time hooks

    def on_node_end(
        self,
        node_name: str,
        input_snap: dict[str, Any],
        output_snap: dict[str, Any] | None,
        duration_ms: float,
        exc: Exception | None,
        is_interrupt: bool = False,
        llm_usage: LLMUsage | None = None,
    ) -> None:
        with self._lock:
            step_idx = self._step_index
            self._step_index += 1
            attempt_idx = self._node_attempt_counts.get(node_name, 0)
            self._node_attempt_counts[node_name] = attempt_idx + 1

            # determine status
            status: StepStatus
            if is_interrupt:
                status = "interrupted"
                exc_str = None
            elif exc is not None:
                status = "crashed"
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                exc_str = f"{type(exc).__name__}: {exc}\n{tb}"
            else:
                status = "pass"
                exc_str = None

            # build merged state (input + output, as successor would see it)
            # Use actual reducer functions when available so the merged state
            # matches what LangGraph produces at runtime.
            merged = dict(input_snap)
            if output_snap:
                for k, v in output_snap.items():
                    if k in self.reducer_fields and k in merged:
                        try:
                            merged[k] = self.reducer_fields[k](merged[k], v)
                        except Exception:
                            merged[k] = v
                    else:
                        merged[k] = v

            # structural inspection (skip on crash or interrupt)
            inspection = None
            if status == "pass" and output_snap is not None:
                successor_fns = self._get_successor_fns(node_name)
                current_fn = self.node_fn_registry.get(node_name)
                inspection = inspect_transition(
                    current_node=node_name,
                    output_dict=output_snap,
                    merged_state=merged,
                    successor_fns=successor_fns,
                    strict=self._strict,
                    input_state=input_snap,
                    current_node_fn=current_fn,
                    reducer_fields=self.reducer_fields or None,
                )
                # Latency-correlated degradation checks
                self._check_latency_signals(duration_ms, inspection)
                # Determine raw status from inspection
                _has_failure = inspection.is_silent_failure or inspection.has_tool_failure
                # Only critical-severity semantic signals affect status — a
                # "warning" signal (e.g. an ambiguous suspicious-phrase match)
                # is recorded on the event for visibility but must not alone
                # fail the node. Mirrors how ToolFailure already gates on
                # severity == "critical" for has_tool_failure above.
                _has_signals = any(
                    s.severity == "critical" for s in inspection.semantic_signals
                )

                if _has_failure or _has_signals:
                    # Before blaming this node, check if it's operating on
                    # degraded upstream data. If an upstream node already
                    # failed, this node's failures are a symptom, not cause.
                    degraded_fields, upstream_node = self._check_degraded_input(
                        input_snap,
                        current_node=node_name,
                    )
                    if degraded_fields:
                        status = "degraded_input"
                        inspection.degraded_fields = degraded_fields
                        inspection.degraded_upstream_node = upstream_node
                    elif _has_failure:
                        status = "fail"
                    else:
                        status = "semantic_fail"
                elif status == "pass":
                    # No inspection failures — still check for degraded input
                    # from upstream (e.g. empty fields that weren't flagged)
                    degraded_fields, upstream_node = self._check_degraded_input(
                        input_snap,
                        current_node=node_name,
                    )
                    if degraded_fields:
                        status = "degraded_input"
                        inspection.degraded_fields = degraded_fields
                        inspection.degraded_upstream_node = upstream_node

            # semantic validation (skip on crash/interrupt)
            validator_results: list[ValidatorResult] = []
            if output_snap is not None and status in ("pass", "fail"):
                validator_results = self._run_validators(
                    node_name,
                    output_snap,
                )
                if any(r.is_blocking for r in validator_results) and status == "pass":
                    status = "semantic_fail"

            # behavioral anomaly detection (runs after heuristic/inspection)
            behavior_type_val: str | None = None
            anomaly_signals: list[AnomalySignal] = []
            if status in ("pass", "fail", "semantic_fail") and output_snap is not None:
                behavior_type_val, anomaly_signals = detect_anomalies(
                    node_name,
                    output_snap,
                    self._behavior_config,
                    input_state=input_snap,
                )
                if any(a.severity == "critical" for a in anomaly_signals) and status == "pass":
                    status = "semantic_fail"

            # Per-node LLM judge: fire in background thread, apply in _finalize.
            # Deterministic status is recorded now; LLM can refine it later.
            semantic_check_result: SemanticCheckResult | None = None
            disambiguation_results: list[DisambiguationResult] = []
            _should_run_judge = (
                output_snap is not None
                and input_snap
                and self._llm_investigation_config
                and self._llm_investigation_config.enabled
                and self._llm_investigation_config.semantic_check
                and status not in ("crashed", "interrupted")
            )
            _deferred_judge = False
            if _should_run_judge:
                ambiguous_signals: list[SemanticSignal] = []
                if (
                    inspection is not None
                    and inspection.semantic_signals
                    and self._llm_investigation_config.heuristic_disambiguation
                    and status not in ("degraded_input",)
                ):
                    conf_lo = self._llm_investigation_config.disambiguation_confidence_low
                    conf_hi = self._llm_investigation_config.disambiguation_confidence_high
                    ambiguous_signals = [
                        s
                        for s in inspection.semantic_signals
                        if conf_lo <= s.confidence <= conf_hi
                    ]

                if self._on_judge_failure == "abort":
                    # Synchronous path: abort mode needs to raise mid-pipeline
                    semantic_check_result, disambiguation_results = (
                        self._run_judge_sync(
                            node_name, input_snap, output_snap,
                            validator_results, anomaly_signals,
                            inspection, ambiguous_signals,
                        )
                    )
                    status = self._apply_judge_verdict(
                        status, semantic_check_result, disambiguation_results,
                        inspection, validator_results, anomaly_signals,
                        node_name, behavior_type_val, output_snap,
                        input_snap=input_snap,
                    )
                else:
                    # Async path: fire LLM in background, don't block
                    _deferred_judge = True

            event = NodeEvent(
                step_index=step_idx,
                node_name=node_name,
                status=status,
                input_state=input_snap,
                output_dict=output_snap,
                duration_ms=round(duration_ms, 2),
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                exception=exc_str,
                inspection=inspection,
                attempt_index=attempt_idx,
                validator_results=validator_results,
                llm_usage=llm_usage,
                behavior_type=behavior_type_val,
                anomaly_signals=anomaly_signals,
                semantic_check=semantic_check_result,
                disambiguation_results=disambiguation_results,
            )

            self._events.append(event)

            # Fire deferred LLM judge in background thread
            if _deferred_judge:
                if self._judge_pool is None:
                    self._judge_pool = ThreadPoolExecutor(
                        max_workers=4, thread_name_prefix="argus-judge"
                    )
                future = self._judge_pool.submit(
                    self._run_judge_sync,
                    node_name, input_snap, output_snap,
                    validator_results, anomaly_signals,
                    inspection, ambiguous_signals,
                )
                with self._pending_judges_lock:
                    self._pending_judges.append(_PendingJudge(
                        event=event,
                        future=future,
                        inspection=inspection,
                        validator_results=validator_results,
                        anomaly_signals=anomaly_signals,
                        ambiguous_signals=ambiguous_signals,
                        deterministic_status=status,
                        node_name=node_name,
                        input_snap=input_snap,
                        output_snap=output_snap,
                    ))

            # Track terminal node completion for parallel-aware finalization
            if node_name in self._terminal_nodes:
                self._completed_terminals.add(node_name)

            # auto-finalize decision (atomic with event append)
            # Uses terminal-node tracking: finalize only when ALL expected DAG
            # leaves have completed. For conditional graphs, unchosen branch
            # terminals don't block finalization.
            should_finalize = (not self._defer_auto_finalize) and (
                status in ("crashed", "interrupted")
                or (
                    not self._is_cyclic
                    and self._terminal_nodes
                    and self._completed_terminals >= self._expected_terminals()
                )
            )

        # finalize outside the lock to avoid holding it during I/O
        if should_finalize:
            self._finalize()

    def _run_validators(self, node_name: str, output_snap: dict) -> list[ValidatorResult]:
        results: list[ValidatorResult] = []
        # wildcard runs first, then node-specific
        for key in ("*", node_name):
            fn = self._validators.get(key)
            if fn is None:
                continue
            fn_name = getattr(fn, "__name__", "lambda")
            vname = f"{key}:{fn_name}"
            try:
                is_valid, message, severity = _parse_validator_return(fn(output_snap))
            except Exception as ve:
                is_valid, message, severity = False, f"Validator raised: {ve}", "critical"
            results.append(
                ValidatorResult(
                    validator_name=vname,
                    is_valid=is_valid,
                    message=message,
                    severity=severity,
                )
            )
        return results

    def _run_judge_sync(
        self,
        node_name: str,
        input_snap: dict,
        output_snap: dict,
        validator_results: list[ValidatorResult],
        anomaly_signals: list[AnomalySignal],
        inspection: InspectionResult | None,
        ambiguous_signals: list[SemanticSignal],
    ) -> tuple[SemanticCheckResult | None, list[DisambiguationResult]]:
        """Run the LLM semantic judge synchronously (with retries)."""
        _judge_exc: Exception | None = None
        result: SemanticCheckResult | None = None
        dis_results: list[DisambiguationResult] = []
        for _retry_i in range(1 + self._judge_max_retries):
            try:
                from argus.semantic_checker import (
                    check_semantic_coherence,  # noqa: PLC0415
                )

                result, dis_results = check_semantic_coherence(
                    node_name=node_name,
                    input_state=input_snap,
                    output_dict=output_snap,
                    model=self._llm_investigation_config.semantic_check_model,
                    validator_results=validator_results,
                    anomaly_signals=anomaly_signals,
                    inspection=inspection,
                    ambiguous_signals=ambiguous_signals or None,
                )
                return result, dis_results
            except Exception as _e:
                _judge_exc = _e
                if _retry_i < self._judge_max_retries:
                    time.sleep(self._judge_retry_backoff * (2**_retry_i))

        if _judge_exc is not None:
            if self._on_judge_failure == "abort":
                raise _judge_exc
            if self._on_judge_failure == "warn":
                import logging  # noqa: PLC0415

                logging.getLogger("argus").warning(
                    "Semantic judge failed for node %r: %s",
                    node_name,
                    _judge_exc,
                )
        return None, []

    def _apply_judge_verdict(
        self,
        status: StepStatus,
        semantic_check_result: SemanticCheckResult | None,
        disambiguation_results: list[DisambiguationResult],
        inspection: InspectionResult | None,
        validator_results: list[ValidatorResult],
        anomaly_signals: list[AnomalySignal],
        node_name: str,
        behavior_type_val: str | None,
        output_snap: dict | None,
        input_snap: dict | None = None,
    ) -> StepStatus:
        """Apply LLM disambiguation + coherence verdict to status. Returns new status."""
        if disambiguation_results and inspection is not None:
            dismissed_ids = {
                r.sig_id
                for r in disambiguation_results
                if not r.llm_verdict and r.llm_confidence >= 0.5
            }
            if dismissed_ids:
                inspection.semantic_signals = [
                    s
                    for s in inspection.semantic_signals
                    if s.sig_id not in dismissed_ids
                ]
                inspection.tool_failures = [
                    tf
                    for tf in inspection.tool_failures
                    if not any(
                        d_id in (tf.evidence or "") for d_id in dismissed_ids
                    )
                ]
                inspection.has_tool_failure = any(
                    tf.severity == "critical" for tf in inspection.tool_failures
                )
                inspection.is_silent_failure = bool(
                    inspection.missing_fields or inspection.has_tool_failure
                )
                _has_failure = (
                    inspection.is_silent_failure or inspection.has_tool_failure
                )
                # Same severity gate as the initial status determination —
                # a leftover warning-severity signal shouldn't re-fail a
                # node the disambiguation pass otherwise cleared.
                _has_signals = any(
                    s.severity == "critical" for s in inspection.semantic_signals
                )
                if not _has_failure and not _has_signals:
                    status = "pass"
                elif _has_failure:
                    status = "fail"
                else:
                    status = "semantic_fail"

        if semantic_check_result is not None:
            if not semantic_check_result.evaluated:
                # Judge did not produce a verdict. Keep heuristic status.
                return status
            sc_passed = semantic_check_result.passed
            sc_confident = semantic_check_result.confidence >= 0.7
            if sc_passed and sc_confident:
                _has_structural = inspection and (
                    inspection.is_silent_failure or inspection.has_tool_failure
                )
                _has_placeholder = inspection and any(
                    tf.failure_type == "placeholder_detected"
                    for tf in (inspection.tool_failures or [])
                )
                _has_validator_failures = any(
                    r.is_blocking for r in validator_results
                )
                _has_critical_anomalies = any(
                    a.severity == "critical" for a in anomaly_signals
                )
                _can_override = (
                    not _has_structural
                    and not _has_placeholder
                    and not _has_validator_failures
                    and not _has_critical_anomalies
                )
                if _can_override and status != "pass":
                    status = "pass"
                    try:
                        from argus.feedback_store import record_override  # noqa: PLC0415

                        record_override(
                            run_id=self.run_id,
                            node_name=node_name,
                            override_type="llm_full_override",
                            anomaly_ids=[
                                a.anomaly_id
                                for a in anomaly_signals
                                if a.severity == "critical"
                            ],
                            anomaly_reasons=[
                                a.reason
                                for a in anomaly_signals
                                if a.severity == "critical"
                            ],
                            llm_reason=semantic_check_result.reason,
                            llm_confidence=semantic_check_result.confidence,
                            behavior_type=behavior_type_val or "unknown",
                            output_shape={
                                "key_count": len(output_snap) if output_snap else 0,
                                "depth": _measure_output_depth(output_snap),
                                "total_chars": len(
                                    json.dumps(output_snap, default=str)
                                )
                                if output_snap
                                else 0,
                            },
                            auto_approve_threshold=(
                                self._llm_investigation_config.false_positive_auto_approve_threshold
                                if self._llm_investigation_config
                                else 0.0
                            ),
                        )
                    except Exception:
                        pass
            elif not sc_passed and sc_confident:
                if status == "pass" and not is_legitimate_field_handoff(
                    input_snap, output_snap
                ):
                    status = "semantic_fail"

        return status

    def _apply_deferred_judges(self) -> None:
        """Collect all background LLM judge results and apply to events."""
        with self._pending_judges_lock:
            pending = list(self._pending_judges)
            self._pending_judges.clear()

        for pj in pending:
            try:
                result = pj.future.result(timeout=15)
                semantic_check_result, disambiguation_results = result
            except Exception:
                semantic_check_result, disambiguation_results = None, []

            new_status = self._apply_judge_verdict(
                pj.deterministic_status,
                semantic_check_result,
                disambiguation_results,
                pj.inspection,
                pj.validator_results,
                pj.anomaly_signals,
                pj.node_name,
                pj.event.behavior_type,
                pj.output_snap,
                input_snap=pj.input_snap,
            )
            pj.event.status = new_status
            pj.event.semantic_check = semantic_check_result
            pj.event.disambiguation_results = disambiguation_results

        if self._judge_pool is not None:
            self._judge_pool.shutdown(wait=False)
            self._judge_pool = None

    def _check_degraded_input(
        self,
        input_snap: dict[str, Any],
        current_node: str | None = None,
    ) -> tuple[list[str], str | None]:
        """Check if any upstream node failed and left fields missing from input.

        Returns (degraded_fields, upstream_node_name) or ([], None).
        Evidence-based: only flags fields that a failed upstream node explicitly
        reported as missing AND are also absent/empty in this node's input.

        A node's OWN earlier iterations are never treated as upstream: in a
        cyclic graph a node re-running and failing again is originating a fresh
        failure, not inheriting degradation from itself. Blaming the earlier
        iteration would demote every repeat failure to ``degraded_input`` and
        hide which iteration actually broke (VAR-105).
        """
        for event in self._events:
            if event.status != "fail" or event.inspection is None:
                continue
            if current_node is not None and event.node_name == current_node:
                continue  # own prior iteration is not upstream degradation
            missing_from_upstream = event.inspection.missing_fields
            if not missing_from_upstream:
                continue
            # Check which of those missing fields are STILL absent in our input
            propagated = [
                f
                for f in missing_from_upstream
                if f not in input_snap or _is_empty_value(input_snap.get(f))
            ]
            if propagated:
                return propagated, event.node_name
        return [], None

    def _get_successor_fns(self, node_name: str) -> list[Any]:
        # ponytail: router nodes fan out to multiple branches but only one runs;
        # validating against all causes false positives — skip them
        if node_name in self._conditional_sources:
            return []
        successors = self.graph_edge_map.get(node_name, [])
        return [self.node_fn_registry[s] for s in successors if s in self.node_fn_registry]

    def _last_expected_node(self) -> str | None:
        return self.graph_node_names[-1] if self.graph_node_names else None

    # ── Loop-aware retries ──────────────────────────────────────────────────

    def _apply_loop_retries(self) -> None:
        """Mark non-final iterations as 'retried' when the loop self-corrected."""
        # ponytail: groups by node_name; O(n) scan, fine for realistic event counts
        from collections import defaultdict

        groups: dict[str, list[int]] = defaultdict(list)
        for i, e in enumerate(self._events):
            groups[e.node_name].append(i)
        for indices in groups.values():
            if len(indices) < 2:
                continue
            total = len(indices)
            for idx in indices:
                self._events[idx].total_iterations = total
            final = self._events[indices[-1]]
            if final.status == "pass":
                for idx in indices[:-1]:
                    self._events[idx].status = "retried"

    # ── Finalization ──────────────────────────────────────────────────────────

    def _finalize(self) -> None:
        # Collect all deferred LLM judge results before aggregating status
        self._apply_deferred_judges()

        with self._lock:
            if self._completed:
                return
            self._completed = True
            self._apply_loop_retries()
            events_snapshot = list(self._events)

        completed_at = datetime.now(timezone.utc).isoformat()

        # Mark nodes on unchosen conditional branches as skipped
        if self._has_conditional_edges and self.graph_node_names:
            executed = {e.node_name for e in events_snapshot}
            for name in self.graph_node_names:
                if name not in executed:
                    events_snapshot.append(
                        NodeEvent(
                            step_index=-1,
                            node_name=name,
                            status="skipped",
                            input_state={},
                            output_dict=None,
                            duration_ms=0.0,
                            timestamp_utc=completed_at,
                        )
                    )

        try:
            start = datetime.fromisoformat(self._started_at)
            end = datetime.fromisoformat(completed_at)
            duration_ms: float | None = (end - start).total_seconds() * 1000
        except Exception:
            duration_ms = None

        # Exclude retried/skipped events — not real failures
        active_events = [e for e in events_snapshot if e.status not in ("retried", "skipped")]
        has_crash = any(e.status == "crashed" for e in active_events)
        has_interrupt = any(e.status == "interrupted" for e in active_events)
        has_silent_failure = any(
            e.inspection and (e.inspection.is_silent_failure or e.inspection.has_tool_failure)
            for e in active_events
        )
        has_semantic_fail = any(e.status == "semantic_fail" for e in active_events)
        has_degraded = any(e.status == "degraded_input" for e in active_events)

        overall_status: RunStatus
        if has_crash:
            overall_status = "crashed"
        elif has_interrupt:
            overall_status = "interrupted"
        elif has_silent_failure or has_semantic_fail or has_degraded:
            overall_status = "silent_failure"
        else:
            overall_status = "clean"

        _fail_statuses = ("fail", "crashed", "semantic_fail", "degraded_input")
        first_failure = next(
            (e.node_name for e in events_snapshot if e.status in _fail_statuses),
            None,
        )

        interrupt_node = next(
            (e.node_name for e in events_snapshot if e.status == "interrupted"),
            None,
        )

        root_cause_chain = build_root_cause_chain(
            events_snapshot,
            self.graph_edge_map,
        )

        coverage_summary = _compute_coverage_summary(
            events_snapshot,
            self.graph_node_names,
        )

        # aggregate LLM metrics
        total_llm_calls = sum(len(e.llm_usage.calls) for e in events_snapshot if e.llm_usage)
        total_tokens = sum(e.llm_usage.total_tokens for e in events_snapshot if e.llm_usage)
        costs = [
            e.llm_usage.total_cost_usd
            for e in events_snapshot
            if e.llm_usage and e.llm_usage.total_cost_usd is not None
        ]
        total_cost_usd = round(sum(costs), 6) if costs else None

        from argus.storage import SCHEMA_VERSION  # noqa: PLC0415

        record = RunRecord(
            run_id=self.run_id,
            argus_version=__version__,
            started_at=self._started_at,
            completed_at=completed_at,
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            overall_status=overall_status,
            first_failure_step=first_failure,
            root_cause_chain=root_cause_chain,
            graph_node_names=self.graph_node_names,
            graph_edge_map=self.graph_edge_map,
            initial_state=self._initial_state,
            steps=events_snapshot,
            schema_version=SCHEMA_VERSION,
            is_cyclic=self._is_cyclic,
            app_factory_ref=self.app_factory_ref,
            node_fn_refs=self.node_fn_refs,
            node_fn_paths=self.node_fn_paths,
            parent_run_id=self.parent_run_id,
            replay_from_step=self.replay_from_step,
            state_patch=self.state_patch,
            interrupted=has_interrupt,
            interrupt_node=interrupt_node,
            total_llm_calls=total_llm_calls,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            behavior_config=self._behavior_config,
            dry_run=self._dry_run,
            coverage_summary=coverage_summary,
        )

        # Load parent run once if this is a replay (reused by correlation + comparison)
        parent_record = None
        if record.parent_run_id:
            try:
                from argus.storage import load_run

                parent_record = load_run(record.parent_run_id)
            except Exception:
                pass

        # Correlation analysis (non-critical — never blocks persistence)
        try:
            from argus.correlator import compare_replay, correlate

            correlation = correlate(record)
            if parent_record:
                try:
                    correlation.replay_impact = compare_replay(record, parent_record)
                except Exception:
                    pass
            record.correlation = correlation

            # Override root_cause_chain with correlation when it has high
            # confidence — the correlator does input→output diffing and is
            # more accurate than the inspector's backward walk which can
            # conflate semantic failures with causal failures.
            # ponytail: only for failed runs — clean/retried runs shouldn't be overridden
            if (
                correlation.degradation_origins
                and record.overall_status not in ("clean", "interrupted")
            ):
                top = correlation.degradation_origins[0]
                if top.confidence >= 0.8:
                    corr_chain = [o.node_name for o in correlation.degradation_origins]
                    record.root_cause_chain = corr_chain
                    record.first_failure_step = corr_chain[0]
        except Exception:
            pass

        # Terminal finding — silent on clean runs (VAR-110). Printed before
        # the optional LLM investigation so the aha is not delayed.
        try:
            from argus.findings import print_run_finding  # noqa: PLC0415

            print_run_finding(record)
        except Exception:
            pass

        # Consolidated per-run LLM call: investigation (absorbs correlation
        # augmentation and loop analysis — one call with full context)
        if self._llm_investigation_config and self._llm_investigation_config.enabled:
            try:
                from argus.llm_investigator import investigate  # noqa: PLC0415

                record.llm_investigation = investigate(
                    record,
                    self._llm_investigation_config,
                )
                if record.llm_investigation and record.llm_investigation.suggested_signatures:
                    from argus.candidate_store import (  # noqa: PLC0415
                        add_candidate,
                        load_candidates,
                        save_candidates,
                    )
                    from argus.signature_generalizer import (  # noqa: PLC0415
                        cluster_with_existing,
                        generalize_signature,
                    )

                    for _sig in record.llm_investigation.suggested_signatures:
                        gen_sig = generalize_signature(_sig)
                        existing = load_candidates()
                        cluster_id = cluster_with_existing(
                            gen_sig,
                            existing.get("candidates", []),
                        )
                        if cluster_id is not None:
                            _merge_candidate(
                                existing,
                                cluster_id,
                                gen_sig,
                                record.run_id,
                            )
                            save_candidates(existing)
                        else:
                            add_candidate(gen_sig, record.run_id)
            except Exception:
                pass

        # Tool chain analysis — deterministic, always-on
        try:
            from argus.tool_chain_analyzer import analyze_tool_chains

            record.tool_chain_findings = analyze_tool_chains(record)
        except Exception:
            pass

        # Apply redaction / state stripping before persisting to disk
        if not self._persist_state:
            record.initial_state = {}
            for step in record.steps:
                step.input_state = {}
                step.output_dict = None
        elif self._redact_keys:
            record.initial_state = self._redact(record.initial_state)
            for step in record.steps:
                step.input_state = self._redact(step.input_state)
                if step.output_dict is not None:
                    step.output_dict = self._redact(step.output_dict)

        # Dry-run gate — skip all persistence (VAR-75)
        if self._dry_run:
            return

        # Sampling gate — skip persistence for sampled-out clean runs (VAR-71)
        import random  # noqa: PLC0415

        is_failure = overall_status != "clean"
        should_persist = (
            self._sample_rate >= 1.0
            or (is_failure and self._persist_failures)
            or random.random() < self._sample_rate
        )
        if not should_persist:
            return

        try:
            save_run(record)
        except Exception as exc:
            warnings.warn(
                f"[argus] Failed to save run {record.run_id}: {exc}. "
                "Check that the working directory is writable.",
                stacklevel=2,
            )

    def finalize(self) -> None:
        """Persist the run record.

        Optional for ArgusWatcher — invoke/ainvoke persist automatically.
        Still the explicit flush for standalone ArgusSession pipelines.
        Idempotent: a second call is a no-op.
        """
        self._finalize()
        atexit.unregister(self._atexit_finalize)

    def force_finalize(self) -> None:
        """Alias for finalize() — used by legacy code and replay engine."""
        self._finalize()

    def begin_new_run(self) -> None:
        """Reset session state so the next invoke() is recorded as a fresh run.

        ArgusWatcher reuses one session across repeated invoke() calls (its node
        wrappers close over it). Without this reset the first run sets
        ``_completed`` and every later finalize() is a no-op — only the first
        run would ever be persisted. Called by the watcher when the outermost
        invoke()/stream()/batch() begins on an already-completed session.
        """
        with self._lock:
            self.run_id = generate_run_id()
            self.parent_run_id = None
            self.replay_from_step = None
            self._events = []
            self._step_index = 0
            self._initial_state = {}
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._completed = False
            self._defer_auto_finalize = False
            self._node_attempt_counts = {}
            self._completed_terminals = set()
        with self._pending_judges_lock:
            self._pending_judges.clear()
        atexit.register(self._atexit_finalize)

    def reset_for_resume(self, parent_run_id: str) -> None:
        """Reset session state so post-interrupt steps are captured in a new run record.

        Called by ArgusWatcher.resume() before re-invoking the graph.
        The new run record will have parent_run_id set so cmd_show can stitch
        the chain together across the interrupt boundary.
        """
        with self._lock:
            self.run_id = generate_run_id()
            self.parent_run_id = parent_run_id
            self._events = []
            self._step_index = 0
            self._initial_state = {}
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._completed = False
            self._defer_auto_finalize = False
            self._node_attempt_counts = {}
            self._completed_terminals = set()
