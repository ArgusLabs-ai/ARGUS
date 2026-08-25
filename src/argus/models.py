from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ValidatorResult:
    validator_name: str  # e.g. "*:check_length" or "summarize:my_fn"
    is_valid: bool
    message: str
    # "ok" | "warning" | "critical" — warnings do not fail the node or CI gate
    severity: str = "critical"

    @property
    def is_blocking(self) -> bool:
        return (not self.is_valid) and self.severity != "warning"


@dataclass(frozen=True)
class SemanticCheckResult:
    """Result of the per-node LLM semantic coherence check."""

    passed: bool
    reason: str  # why it passed or failed (from the LLM)
    confidence: float  # 0.0–1.0
    model: str  # e.g. "gpt-4o-mini"
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float
    # Audit trail: which prior signals the LLM explicitly considered
    evidence_considered: tuple[str, ...] = ()
    # Which prior signals the LLM chose to override (pass despite signal)
    overridden_signals: tuple[str, ...] = ()
    # False when the judge never actually ran (not logged in, empty I/O,
    # provider error, timeout) — `passed` defaults True in that case for
    # backward-compat verdict behavior, but callers that need to know
    # whether a real judgment happened must check this instead.
    evaluated: bool = True


@dataclass
class FieldMismatch:
    field_name: str
    expected_type: str
    actual_type: str
    actual_value_repr: str


@dataclass
class ToolFailure:
    # "error_response" | "rate_limit" | "empty_result" | "error_in_data" | "partial_failure"
    failure_type: str
    field_name: str  # which key in the output dict triggered detection
    severity: str  # "critical" | "warning"
    evidence: str  # short human-readable description of what was found


@dataclass(frozen=True)
class SemanticSignal:
    """A semantic degradation signal emitted by the heuristic engine."""

    sig_id: str  # e.g. "PH-001", "CM-003"
    category: str  # e.g. "placeholder_outputs"
    severity: str  # "critical" | "warning"
    description: str
    field_path: tuple[str, ...]  # e.g. ("result", "items", "[0]", "summary")
    evidence: str  # ≤80 char snippet of the matched value
    confidence: float = 1.0  # 0.0–1.0, how confident the heuristic match is

    @property
    def dotted_path(self) -> str:
        return ".".join(self.field_path)


@dataclass(frozen=True)
class AnomalySignal:
    """A behavioral anomaly signal emitted by the anomaly detector."""

    anomaly_id: str  # e.g. "BA-001"
    severity: str  # "critical" | "warning"
    suspicion_score: float  # 0.0–1.0
    reason: str  # human-readable explanation
    expected_behavior: str  # what was expected for the behavior type
    observed_behavior: str  # what was actually observed
    field_path: str  # dotted path in output, or "" for whole output


@dataclass(frozen=True)
class ToolChainFinding:
    """A cross-node tool usage anti-pattern detected across the run."""

    finding_id: str  # "TC-001" through "TC-004"
    # "retry_storm" | "ordering_anomaly" | "unused_result" | "argument_degradation"
    finding_type: str
    severity: str  # "critical" | "warning"
    nodes_involved: tuple[str, ...]
    description: str
    evidence: str  # short supporting data
    confidence: float  # 0.0–1.0


@dataclass
class BehaviorConfig:
    """Pipeline-level behavior configuration."""

    default_behavior_type: str | None = None
    node_behaviors: dict[str, str] = field(default_factory=dict)


@dataclass
class InspectionResult:
    is_silent_failure: bool
    missing_fields: list[str]
    empty_fields: list[str]
    type_mismatches: list[FieldMismatch]
    severity: str  # "critical" | "warning" | "info" | "ok"
    message: str
    unannotated_successors: list[str] = field(default_factory=list)
    suspicious_empty_keys: list[str] = field(default_factory=list)
    tool_failures: list[ToolFailure] = field(default_factory=list)
    has_tool_failure: bool = False  # True if any tool_failures with severity="critical"
    semantic_signals: list[SemanticSignal] = field(default_factory=list)
    # Upstream propagation: fields missing from input because an upstream node failed
    degraded_fields: list[str] = field(default_factory=list)
    degraded_upstream_node: str | None = None  # which upstream node caused it


@dataclass
class LLMCallInfo:
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None = None


@dataclass
class LLMUsage:
    calls: list[LLMCallInfo] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float | None = None


@dataclass(frozen=True)
class DisambiguationResult:
    """Result of LLM disambiguation for an ambiguous heuristic match."""

    sig_id: str
    field_path: str  # dotted path of the signal
    original_confidence: float
    llm_verdict: bool  # True = confirmed failure, False = false positive
    llm_confidence: float  # 0.0–1.0
    llm_reason: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float


@dataclass
class NodeEvent:
    step_index: int
    node_name: str
    # "pass" | "fail" | "crashed" | "degraded_input" | "semantic_fail"
    # | "interrupted" | "retried"
    status: str
    input_state: dict[str, Any]
    output_dict: dict[str, Any] | None
    duration_ms: float
    timestamp_utc: str
    exception: str | None = None
    inspection: InspectionResult | None = None
    attempt_index: int = 0  # how many times this node has run before this event (0-indexed)
    validator_results: list[ValidatorResult] = field(default_factory=list)
    is_subgraph_entry: bool = False  # True if this node is a compiled subgraph
    subgraph_run_id: str | None = None  # run_id of the child session for subgraph nodes
    llm_usage: LLMUsage | None = None
    behavior_type: str | None = None
    anomaly_signals: list[AnomalySignal] = field(default_factory=list)
    semantic_check: SemanticCheckResult | None = None
    disambiguation_results: list[DisambiguationResult] = field(default_factory=list)
    total_iterations: int | None = None  # set on finalize for looped nodes


# ── Replay comparison dataclasses ─────────────────────────────────────────────


@dataclass
class NodeDiffSummary:
    """Per-node natural language summary of what changed between original and replay."""

    node_name: str
    status_before: str
    status_after: str
    summary: str
    verdict: str  # "fixed" | "regressed" | "unchanged" | "changed"


@dataclass
class ReplayComparisonResult:
    """Structured LLM comparison of a replay run vs its parent."""

    structural_summary: str
    failure_analysis: str
    root_cause_delta: str
    key_insights: list[str]
    recommendation: str
    confidence: float
    node_summaries: list[NodeDiffSummary] = field(default_factory=list)
    model_used: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class LoopIterationDiff:
    """What changed between two consecutive loop iterations."""

    from_attempt: int
    to_attempt: int
    summary: str  # "Added error handling to parse function"
    fields_changed: list[str] = field(default_factory=list)


@dataclass
class LoopAnalysisResult:
    """LLM-generated analysis of a single looped node."""

    node_name: str
    total_iterations: int
    summary: str  # "Took 3 attempts. Attempt 1: syntax error..."
    is_stalled: bool
    stall_details: str | None
    unnecessary_retries: int  # 0 if no wasted iterations
    unnecessary_details: str | None
    iteration_diffs: list[LoopIterationDiff] = field(default_factory=list)
    model_used: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class RunRecord:
    run_id: str
    argus_version: str
    started_at: str
    completed_at: str | None
    duration_ms: float | None
    overall_status: str  # "clean" | "silent_failure" | "crashed"
    first_failure_step: str | None
    root_cause_chain: list[str]
    graph_node_names: list[str]
    graph_edge_map: dict[str, list[str]]
    initial_state: dict[str, Any]
    steps: list[NodeEvent] = field(default_factory=list)
    schema_version: str = "0"  # ponytail: "0"=pre-VAR-71, "1"=current; migrate on load
    parent_run_id: str | None = None
    replay_from_step: str | None = None
    is_cyclic: bool = False  # True if the graph contains back-edges
    subgraph_run_ids: list[str] = field(default_factory=list)  # child run ids
    app_factory_ref: str | None = None  # auto-captured "module:function" for replay
    node_fn_refs: dict[str, str] | None = None  # factory-free replay refs
    node_fn_paths: dict[str, str] | None = None  # {node_name: relative_file_path}
    interrupted: bool = False  # True if a GraphInterrupt occurred
    interrupt_node: str | None = None  # node name where interrupt occurred
    total_llm_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float | None = None
    behavior_config: BehaviorConfig | None = None
    correlation: CorrelationReport | None = None
    llm_investigation: LLMInvestigationResult | None = None
    replay_comparison: ReplayComparisonResult | None = None
    loop_analyses: list[LoopAnalysisResult] = field(default_factory=list)
    tool_chain_findings: list[ToolChainFinding] = field(default_factory=list)
    dry_run: bool = False  # True if this run skipped persistence (VAR-75)
    # Fraction of the run actually evaluated by each detection layer, 0.0–1.0.
    # Keys: "structural", "heuristic", "judge". Empty on old runs / when a layer
    # ran on no nodes. Distinguishes "checked, clean" from "never checked".
    coverage_summary: dict[str, float] = field(default_factory=dict)


# ── Correlation layer dataclasses ──────────────────────────────────────────────


@dataclass(frozen=True)
class PropagationLink:
    """A causal link between two nodes showing how degradation spread."""

    source_node: str
    target_node: str
    signal_type: str  # "field_drop" | "placeholder" | "anomaly_cascade" | "semantic_collapse"
    confidence: float  # 0.0–1.0
    evidence: str  # short human-readable description


@dataclass(frozen=True)
class DegradationOrigin:
    """The most likely starting point of degradation in the pipeline."""

    node_name: str
    step_index: int
    signal_types: tuple[str, ...]  # e.g. ("tool_failure", "missing_field")
    confidence: float  # 0.0–1.0
    reason: str  # human-readable explanation
    # Raw signal contributions that produced the score, e.g. ("tool_failure", 3.0)
    confidence_breakdown: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class PropagationChain:
    """A sequence of nodes showing how degradation spread from an origin."""

    chain_type: str  # "tool_failure_cascade" | "semantic_collapse" | "field_drop_cascade"
    # | "placeholder_propagation" | "anomaly_cascade" | "mixed_degradation"
    nodes: tuple[str, ...]
    links: tuple[PropagationLink, ...]
    summary: str  # developer-readable causal narrative


@dataclass(frozen=True)
class TimelineEvent:
    """A single event in the execution timeline with degradation markers."""

    step_index: int
    node_name: str
    event_type: str  # "node_ok" | "degradation_onset" | "propagation" | "crash"
    label: str  # short description for display
    signal_summary: str  # brief description of signals at this step


@dataclass
class ReplayImpact:
    """Comparison of a replay run against its original."""

    improved_nodes: list[str]
    regressed_nodes: list[str]
    key_fix_node: str | None  # node whose fix most impacted downstream
    downstream_improvement_count: int
    summary: str
    original_failure_node: str | None = None
    original_failure_resolved: bool | None = None  # None if original had no failure


@dataclass(frozen=True)
class LLMCorrelationInsight:
    """LLM-generated insight augmenting the deterministic correlator."""

    enhanced_summary: str  # augments the deterministic causal_summary
    cross_node_connections: tuple[str, ...]  # connections the deterministic correlator missed
    confidence: float
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float
    error: str | None = None


@dataclass
class CorrelationReport:
    """Root-cause correlation analysis for a run."""

    run_id: str
    degradation_origins: list[DegradationOrigin]
    propagation_chains: list[PropagationChain]
    causal_summary: str  # 1–3 sentence developer-readable narrative
    timeline: list[TimelineEvent]
    replay_impact: ReplayImpact | None = None
    llm_insight: LLMCorrelationInsight | None = None


# ── LLM Semantic Investigator dataclasses ─────────────────────────────────────


@dataclass(frozen=True)
class SemanticHypothesis:
    """A causal hypothesis generated by the LLM investigator."""

    hypothesis: str  # concise statement of what may have happened
    confidence: float  # 0.0–1.0 — LLM's self-assessed confidence
    supporting_evidence: tuple[str, ...]  # references to signals/nodes that support this
    category: str  # "retrieval_degradation" | "hallucination_onset" |
    # "semantic_drift" | "reasoning_collapse" |
    # "context_loss" | "tool_failure_cascade" | "other"


@dataclass(frozen=True)
class SuggestedSignature:
    """A new semantic degradation pattern suggested by the LLM for registry review."""

    pattern: str  # the string/regex pattern to match
    match_strategy: str  # "exact_ci" | "contains_ci" | "prefix_ci" | "regex"
    proposed_category: str  # e.g. "placeholder_outputs", "semantic_drift", etc.
    severity: str  # "critical" | "warning"
    description: str  # what this pattern detects
    evidence: tuple[str, ...]  # real examples from the run that triggered this suggestion
    confidence: float  # 0.0–1.0
    reasoning: str  # why the LLM thinks this is a recurring pattern
    original_pattern: str | None = None  # pre-generalization literal
    generalized: bool = False  # True if pattern was auto-generalized


@dataclass
class LLMInvestigationResult:
    """Output of the selective LLM semantic investigator."""

    triggered: bool  # whether investigation actually ran
    trigger_reasons: list[str]  # why investigation was triggered
    root_cause_explanation: str  # semantic explanation of the root cause
    causal_hypotheses: list[SemanticHypothesis]
    degradation_narrative: str  # developer-readable forensic narrative
    cross_node_connections: list[str]  # semantic causal links beyond field-drop tracking
    observations: list[str]  # semantic observations about the execution
    debugging_suggestions: list[str]  # actionable next steps for the developer
    confidence: float  # overall confidence in the analysis (0.0–1.0)
    suggested_signatures: list[SuggestedSignature]  # new patterns for registry review
    model_used: str  # which LLM model was used
    prompt_tokens: int  # tokens consumed
    completion_tokens: int
    investigation_duration_ms: float  # wall-clock time for LLM call
    error: str | None = None  # if investigation failed, why


@dataclass
class LLMInvestigationConfig:
    """Configuration for the LLM semantic investigator."""

    enabled: bool = False
    model: str = "gpt-4o"
    api_key: str | None = None  # unused — all calls go through ARGUS proxy
    max_tokens: int = 2048
    temperature: float = 0.2
    confidence_threshold: float = 0.6  # trigger if deterministic confidence < this
    max_origins_for_ambiguity: int = 2  # trigger if >= this many competing origins
    always_investigate: bool = False  # bypass trigger logic (for debugging)
    suggest_signatures: bool = True  # whether to ask LLM for new patterns
    semantic_check: bool = False  # per-node LLM judge; opt-in with semantic_judge=True
    semantic_check_model: str = "gpt-4o-mini"  # cheap model for per-node checks
    # Auto-resolve LLM overrides above this confidence (0.0 = disabled)
    false_positive_auto_approve_threshold: float = 0.85
    # Heuristic disambiguation — LLM resolves ambiguous signature matches
    heuristic_disambiguation: bool = True
    disambiguation_model: str = "gpt-4o-mini"
    disambiguation_confidence_low: float = 0.3  # below = definite match (no LLM)
    disambiguation_confidence_high: float = 0.7  # above = definite match (no LLM)
    # LLM-assisted correlation — augments deterministic correlator
    llm_correlation: bool = True
    correlation_model: str = "gpt-4o"
    correlation_max_tokens: int = 1500


@dataclass
class ArgusConfig:
    """Typed configuration for ArgusWatcher / ArgusSession.

    Consolidates all monitoring parameters into a single validated object.
    Pass to ArgusWatcher(config=...) or ArgusSession(config=...).
    Individual kwargs are still accepted for backward compatibility.
    """

    max_field_size: int = 50_000
    strict: bool = False
    investigate: bool | str = True  # True | False | "always"
    redact_keys: set[str] | list[str] | None = None
    redact_functions: dict[str, Callable[[Any], Any]] | None = None
    redact_patterns: bool = False  # auto-detect secret-shaped values
    persist_state: bool = True
    record_http: bool = True
    semantic_judge: bool = False  # opt-in; heuristics-only until semantic_judge=True
    judge_model: str = "gpt-4o"
    # Failure policy for the per-node semantic judge LLM call.
    # "warn"  — log warning, continue with heuristic results (default)
    # "skip"  — silently continue, no log
    # "abort" — re-raise the exception, failing the node
    on_judge_failure: str = "warn"
    judge_max_retries: int = 1  # ponytail: bump to 2-3 if transient failures are common
    judge_retry_backoff: float = 0.5  # seconds, doubled each retry
    # Run persistence sampling — persist only a fraction of runs to save disk (VAR-71)
    sample_rate: float = 1.0  # 0.0–1.0, fraction of clean runs to persist
    persist_failures: bool = True  # always persist runs with non-clean status
    dry_run: bool = False  # skip persistence entirely (VAR-75)
    # Latency-correlated degradation thresholds (VAR-8)
    node_timeout_ms: float | None = None  # e.g. 30000 — flag outputs at >=95% of this
    min_expected_ms: float | None = None  # e.g. 500 — flag suspiciously fast LLM nodes

    def __post_init__(self) -> None:
        """Cross-validate parameter combinations — fail fast with clear messages (VAR-73)."""
        errors: list[str] = []

        # ── Type/range checks ────────────────────────────────────────────
        if self.max_field_size <= 0:
            errors.append(f"max_field_size must be positive, got {self.max_field_size}")

        if self.investigate not in (True, False, "always"):
            errors.append(
                f"investigate must be True, False, or 'always', got {self.investigate!r}"
            )

        _valid_policies = ("warn", "skip", "abort")
        if self.on_judge_failure not in _valid_policies:
            errors.append(
                f"on_judge_failure must be one of {_valid_policies}, got {self.on_judge_failure!r}"
            )

        if self.judge_max_retries < 0:
            errors.append(f"judge_max_retries must be >= 0, got {self.judge_max_retries}")

        if self.judge_retry_backoff <= 0:
            errors.append(f"judge_retry_backoff must be positive, got {self.judge_retry_backoff}")

        if not (0.0 <= self.sample_rate <= 1.0):
            errors.append(f"sample_rate must be between 0.0 and 1.0, got {self.sample_rate}")

        # ── Cross-parameter checks ───────────────────────────────────────
        if not self.persist_state and self.investigate == "always":
            errors.append(
                "investigate='always' with persist_state=False: LLM investigation "
                "requires state data to analyze. Either set persist_state=True or "
                "set investigate=True (auto) / False."
            )

        if self.semantic_judge and not self.investigate:
            errors.append(
                "semantic_judge=True requires investigate to be truthy. "
                "Set investigate=True or 'always', or disable semantic_judge."
            )

        if self.sample_rate == 0.0 and not self.persist_failures:
            errors.append(
                "sample_rate=0.0 with persist_failures=False: no runs will ever "
                "be persisted. Set persist_failures=True or increase sample_rate."
            )

        if errors:
            raise ValueError("ArgusConfig validation failed:\n  - " + "\n  - ".join(errors))
