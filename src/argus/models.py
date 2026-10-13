from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal

from argus.claim_models import ClaimVerdict
from argus.failure_class import classify_failure_class, status_from_text, valid_failure_class

# Closed status vocabularies. Documented in docs/STATUS.md — change both together.
StepStatus = Literal[
    "pass",
    "fail",
    "crashed",
    "semantic_fail",
    "degraded_input",
    "interrupted",
    "retried",
    "skipped",
]
RunStatus = Literal["clean", "crashed", "interrupted", "silent_failure"]


FindingSource = Literal["heuristic", "validator", "anomaly", "llm", "crash"]


@dataclass(frozen=True)
class Finding:
    """One normalized failure signal on a run. Flat, stable, consumer-facing.

    Built at finalize by ``argus.findings.collect_findings`` from the per-step
    inspection / validator / anomaly / judge data, so downstream consumers
    (``argus check --format json``, exporters, the dashboard) read one list
    instead of walking every step shape. See docs/STATUS.md.
    """

    id: str  # stable content hash: sha1(type|node|field_path|source)[:10]
    node: str
    type: str  # e.g. "missing_field", "empty_output", "placeholder_outputs", "crash"
    severity: str  # "critical" | "warning" | "info"
    reason: str  # full sentence readable without seeing the node
    source: FindingSource
    field_path: str | None = None
    origin_node: str | None = None  # upstream culprit when known
    confidence: float | None = None
    suppressed: bool = False
    # Retryability class of a tool-failure finding ("fatal" | "transient" |
    # "rate_limited"); None for every other finding. Last on purpose: records
    # saved before it existed load with the default.
    failure_class: str | None = None


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


@dataclass
class JudgeScore:
    """One scored judgment from a name-bound validator (PRD-09), kept beside its
    ``ValidatorResult`` on ``RunRecord.judge_scores``.

    ``score`` is in [0, 1]; ``direction`` says which end is good, so runs rank
    consistently for "maximize" and "minimize" judges. ``severity`` is ``ok`` or
    ``warning`` only: a score below the floor is advisory and never gates. A
    judge may return one of these directly; the session stamps ``validator_name``,
    ``origin_node`` and ``step_index`` itself.
    """

    score: float
    label: str = ""
    explanation: str = ""
    direction: Literal["maximize", "minimize"] = "maximize"
    validator_name: str = ""  # e.g. "*:judge" — same key as ValidatorResult
    severity: str = "ok"  # "ok" | "warning" (under the floor)
    origin_node: str | None = None
    step_index: int | None = None


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
    # Why the judge failed the node, in its own words, from a fixed vocabulary:
    # "unrelated"        — the output is about something other than the input
    # "contradiction"    — the output contradicts the input or itself
    # "empty_or_missing" — a field is blank, null or absent
    # "other"            — anything else, and the default for an older record
    #
    # Audit only. None of these kinds fail `argus check` on their own —
    # the judge reviews soft flags and cannot originate a fail.
    failure_kind: str = "other"


# Kept so older records and tests can name the kinds. None of them gate CI.
JUDGE_STANDALONE_FAILURE_KINDS = frozenset({"unrelated", "contradiction"})
JUDGE_STANDALONE_MIN_CONFIDENCE = 0.9


# ── Behavior-contract verdict algebra ─────────────────────────────────────────
#
# Three-valued on purpose. A boolean judge forces "the run never showed this
# conduct" into either pass (wrong — absent evidence is not compliance) or
# fail (wrong — the run may have been cut short before the conduct was due).
# NA carries *why* it is not a verdict, because "this contract does not apply
# to this run" and "the trace is too thin to tell" must not be scored the
# same way. The fold is deterministic Python: the judge never aggregates its
# own clauses — aggregation is code, so it can be audited and unit-tested.


class BehaviorVerdict(str, Enum):
    """Per-clause judgment from the behavior judge."""

    TRUE = "true"  # the agent did what the clause requires
    FALSE = "false"  # the agent violated the clause (requires citations)
    NA = "na"  # no verdict: partial run or genuinely unjudgeable clause


# Why a judgment is NA. "not_applicable" is a property of the contract ("this
# clause only binds runs that take the refund path"), the other two are
# properties of the evidence (partial run, or conduct that cannot be observed
# in a recorded trace at all). NA without one of these reasons is invalid.
NAReason = Literal["not_applicable", "insufficient_evidence", "behavior_not_judgeable"]
NA_REASONS = frozenset({"not_applicable", "insufficient_evidence", "behavior_not_judgeable"})

# Why a run ended — drives the complete-run absence rule in the behavior
# judge. None on a clean record that predates the field; a crashed or
# interrupted one gets the matching reason on load (storage._run_completeness).
# "unknown": nobody told ARGUS the run was over — it was finalized at process
# exit and nothing proves it had reached its end, so it may have been cut off
# anywhere: there was no topology, the graph is conditional (its edge map cannot
# say which branch a router took), or an edge map names a node that never ran.
# "cancelled": the caller abandoned a streamed run (ArgusWatcher)
# before its last node ran, or the user aborted it (KeyboardInterrupt).
EndReason = Literal["finished", "exception", "interrupted", "cancelled", "unknown"]

_TRUE_STRINGS = frozenset({"true", "yes", "1", "pass", "passed"})
_FALSE_STRINGS = frozenset({"false", "no", "0", "fail", "failed", "violation", "violated"})
_NA_STRINGS = frozenset({"na", "n/a", "not_applicable", "not-applicable", "none", "unknown"})


def coerce_behavior_verdict(value: Any) -> BehaviorVerdict | None:
    """Read a judge's three-valued verdict, or None if it isn't one.

    Mirrors ``semantic_checker._coerce_verdict``'s robustness (real booleans,
    the string forms models emit under JSON mode, the 0/1 integers they
    substitute for booleans) extended to NA. ``bool()`` is the wrong tool for
    the same reason as the two-valued version: bool("false") is True. A
    missing, null, or confidence-shaped value means the judge gave no verdict
    and the caller must skip rather than invent one.
    """
    if isinstance(value, BehaviorVerdict):
        return value
    if isinstance(value, bool):
        return BehaviorVerdict.TRUE if value else BehaviorVerdict.FALSE
    if isinstance(value, int) and value in (0, 1):
        return BehaviorVerdict.TRUE if value else BehaviorVerdict.FALSE
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_STRINGS:
            return BehaviorVerdict.TRUE
        if s in _FALSE_STRINGS:
            return BehaviorVerdict.FALSE
        if s in _NA_STRINGS:
            return BehaviorVerdict.NA
    return None


@dataclass(frozen=True)
class BehaviorJudgment:
    """One judge verdict for one clause of one behavior spec.

    ``evaluated=False`` marks every path where no real judgment happened —
    judge unavailable, unrepairable payload, an uncited or unquoted FALSE, an
    insufficient-evidence NA on a complete run, a TRUE on a trajectory view
    that omits steps. Unevaluated judgments are excluded from the fold and
    every score/count: fail closed to *unjudged*, never to pass.
    """

    behavior: str  # the spec's name
    clause: str  # the clause heading this judgment is for
    verdict: BehaviorVerdict
    na_reason: str | None  # required when verdict is NA; must be in NA_REASONS
    confidence: float  # 0.0–1.0
    cited_steps: tuple[int, ...]  # step_index values into the run's steps
    # The model's own quote behind a FALSE, whitespace-collapsed but otherwise as given: it
    # may carry Markdown or curly quotes the clause text does not (the judge matched it
    # against the clause body on a normalized form), so it is not a slice of the clause.
    violated_clause: str | None
    reason: str  # the judge's one-sentence justification
    judge_model: str
    evaluated: bool = True
    # sha256[:16] of the clause text this was judged against; "" on a judgment
    # that predates the field. A re-run keeps an earlier verdict only while the
    # clause still hashes to this — a rewritten clause is a different contract.
    clause_hash: str = ""


def fold_behavior_verdicts(
    judgments: list[BehaviorJudgment] | tuple[BehaviorJudgment, ...],
) -> BehaviorVerdict:
    """Deterministic fold over one behavior's clause judgments.

    Any evaluated FALSE ⇒ FALSE (one violated clause breaks the contract —
    clauses are individually judgeable so the contract reads as a
    conjunction). Else, if any clause is unevaluated, or is an evaluated NA
    for ``insufficient_evidence`` ⇒ NA: part of the contract could not be
    judged for want of evidence and may hide a violation, so the behavior
    cannot be called satisfied. Else, if every judgment is NA (or there are
    none) ⇒ NA: with no evidence the only honest answer is "not a verdict",
    never TRUE. Else TRUE: an NA that says the clause does not apply
    (``not_applicable``) or cannot be observed in any trace
    (``behavior_not_judgeable``) is left out of the score, because nothing
    was hidden. The judge never performs this fold — it is pure Python so the
    aggregation is auditable and testable, not prompt-interpretable.
    """
    if any(j.evaluated and j.verdict is BehaviorVerdict.FALSE for j in judgments):
        return BehaviorVerdict.FALSE
    if not judgments or any(
        not j.evaluated
        or (j.verdict is BehaviorVerdict.NA and j.na_reason == "insufficient_evidence")
        for j in judgments
    ):
        return BehaviorVerdict.NA
    if all(j.verdict is BehaviorVerdict.NA for j in judgments):
        return BehaviorVerdict.NA
    return BehaviorVerdict.TRUE


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
    # Retryability class ("fatal" | "transient" | "rate_limited", see
    # argus.failure_class). Additive: never changes failure_type or severity.
    # The inspector passes it where the status / error text is in scope; an
    # empty or unrecognised value is derived from failure_type + the HTTP status
    # in the evidence, so records saved before it existed load with a class.
    failure_class: str = ""

    def __post_init__(self) -> None:
        # Never raises: a ToolFailure is built on every graded step and on every
        # run load, and an unknown failure_type must not make a run unreadable.
        self.failure_class = valid_failure_class(self.failure_class) or classify_failure_class(
            self.failure_type, status_from_text(self.evidence), self.evidence
        )


@dataclass(frozen=True)
class CheckVerdict:
    """Result of one user-defined custom semantic check (YAML/JSON spec).

    Advisory only — the reviewer role applies to custom checks exactly as it
    does to the coherence judge: a failing check surfaces as a semantic
    signal for the report, never as a gate failure on its own.
    """

    check_name: str  # the spec's ``name``
    verdict: str  # the choice key the judge picked (or "pass"/"fail")
    score: float | None  # 0.0–1.0 from the spec's choice table; None for pass/fail specs
    passed: bool
    reason: str
    severity: str  # "critical" | "warning" | "info" — the spec's severity
    prompt_tokens: int
    completion_tokens: int
    # False when the judge never actually ran (no key, provider error,
    # unparseable verdict) — mirrors SemanticCheckResult.evaluated.
    evaluated: bool = True


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
    has_tool_warnings: bool = False  # True if any tool_failures with severity="warning"
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
    # Why the model stopped: "stop", "length" (OpenAI), "max_tokens" (Anthropic)...
    finish_reason: str | None = None
    # What the model returned, clipped. A node that falls back to a default after
    # the model's JSON failed to parse leaves no other trace (trace_rules D11).
    output_text: str | None = None


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
    status: StepStatus  # see docs/STATUS.md
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
    # Tool I/O recorded for this step: {name, input, output, error}. Set by
    # ArgusRecorder from LangGraph's tool callbacks; the ledger's tool column.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Where the node routed itself, when it handed off with a `Command` (#110).
    # The route actually taken, not the branches it could have taken — a
    # dynamic `goto` is invisible to `get_graph`, so this is the only record
    # of why the next node ran. Empty for a node that returned a plain update.
    goto: list[str] = field(default_factory=list)
    # The Pregel superstep this step ran in, as "<parent checkpoint ns>#<step>".
    # Parallel `Send` workers share it; loop iterations do not. None when the
    # trace carries no step metadata (trace-file ingest, the wrap path).
    superstep: str | None = None
    # What the run reviewer verified on this step (argus.review): dicts with
    # kind / claim / why / correction / role ("confirms" | "promoted" | "advisory").
    review: list[dict[str, Any]] = field(default_factory=list)
    # Hits silenced by `argus ignore` (project .argus/config.json). Kept for stats
    # and findings; they do not affect status. See suppressions.py.
    suppressed_signals: list[SemanticSignal] = field(default_factory=list)
    suppressed_anomalies: list[AnomalySignal] = field(default_factory=list)


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
    overall_status: RunStatus  # see docs/STATUS.md for the node → run roll-up
    first_failure_step: str | None
    root_cause_chain: list[str]
    graph_node_names: list[str]
    graph_edge_map: dict[str, list[str]]
    initial_state: dict[str, Any]
    # {field: "add"|"overwrite"} — how the state schema combines a field, as a
    # string, so the ledger folds a reloaded run the same way it folds a live
    # one. Reducer callables cannot be persisted. See ledger.reducer_kinds.
    reducer_kinds: dict[str, str] = field(default_factory=dict)
    # The keys this graph's state actually has. A subgraph node writes into its
    # own schema, and a key that lives only there never reaches the parent state
    # — so the ledger must not carry it forward as though a later node could
    # read it. Empty means unknown, and the ledger folds everything as before.
    state_keys: list[str] = field(default_factory=list)
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
    # Patch applied to the replayed node's input state before this run (time-travel)
    state_patch: dict[str, Any] | None = None
    total_llm_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float | None = None
    behavior_config: BehaviorConfig | None = None
    correlation: CorrelationReport | None = None
    llm_investigation: LLMInvestigationResult | None = None
    replay_comparison: ReplayComparisonResult | None = None
    loop_analyses: list[LoopAnalysisResult] = field(default_factory=list)
    tool_chain_findings: list[ToolChainFinding] = field(default_factory=list)
    # Cross-step claim verdicts (PRD-10) appended by `argus claims <run-id>`
    # (read-modify-write, like `behavior_judgments`). Audit data: a broken one
    # surfaces as a `broken_claim` warning finding, never as a gate input.
    claim_verdicts: list[ClaimVerdict] = field(default_factory=list)
    # Flat, normalized list of every failure signal on the run (schema_version >= "2").
    # Back-filled on load for older records. See docs/STATUS.md and findings.py.
    findings: list[Finding] = field(default_factory=list)
    dry_run: bool = False  # True if this run skipped persistence (VAR-75)
    # Fraction of the run actually evaluated by each detection layer, 0.0–1.0.
    # Keys: "structural", "heuristic", "judge". Empty on old runs / when a layer
    # ran on no nodes. Distinguishes "checked, clean" from "never checked".
    coverage_summary: dict[str, float] = field(default_factory=dict)
    # Completeness header for the behavior judge's absence rule. complete=False
    # means the trace does not cover the whole run (exception/interrupt), so
    # missing observable conduct folds to NA (insufficient_evidence) instead of
    # FALSE. Absent in the ~4700 pre-field run files: load_run derives it from
    # overall_status / interrupted (a crashed or interrupted run is not
    # complete; the rest are, and keep the strict complete-run rule).
    # schema_version stays "2" — both fields are optional-with-defaults, which
    # is the compatibility layer.
    complete: bool = True
    # None | "finished" | "exception" | "interrupted" | "cancelled" | "unknown"
    end_reason: str | None = None
    # Behavior-contract judgments appended by `argus behavior judge`
    # (read-modify-write, the same precedent as `argus locate`). Round-trips
    # through save_run/load_run; excluded from every score and gate.
    behavior_judgments: list[BehaviorJudgment] = field(default_factory=list)
    # One JudgeScore per scored judge invocation (PRD-09), stamped at the end of
    # the run. A display/ranking surface (`argus stats <run>`, `argus diff`) and
    # the source of `low_judge_score` findings; never a gate input. Default []
    # loads records written before the field existed.
    judge_scores: list[JudgeScore] = field(default_factory=list)


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
    # A judge score under this floor is `low_judge_score` (warning, never a gate);
    # for "minimize" judges the bad end is above 1 - floor (PRD-09).
    judge_score_floor: float = 0.5

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

        if not (0.0 <= self.judge_score_floor <= 1.0):
            errors.append(
                f"judge_score_floor must be between 0.0 and 1.0, got {self.judge_score_floor}"
            )

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
