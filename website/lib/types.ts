export type RunStatus = 'clean' | 'silent_failure' | 'crashed' | 'semantic_fail' | 'interrupted'
export type StepStatus = 'pass' | 'fail' | 'crashed' | 'semantic_fail' | 'interrupted' | 'degraded_input' | 'retried' | 'skipped'
export type Severity = 'critical' | 'warning' | 'info' | 'ok'
export type BehaviorType =
  | 'structured_json'
  | 'retrieval_result'
  | 'classification'
  | 'detailed_text'
  | 'tool_output'
  | 'reasoning_chain'
  | 'chat_response'
  | 'code_generation'

export interface SemanticSignal {
  sig_id: string
  category: string
  severity: 'critical' | 'warning'
  description: string
  field_path: string[]
  evidence: string
}

export interface AnomalySignal {
  anomaly_id: string
  severity: 'critical' | 'warning'
  suspicion_score: number
  reason: string
  expected_behavior: string
  observed_behavior: string
  field_path: string
}

export interface BehaviorConfig {
  default_behavior_type: BehaviorType | null
  node_behaviors: Record<string, BehaviorType>
}

export interface ToolFailure {
  failure_type:
    | 'error_response'
    | 'rate_limit'
    | 'empty_result'
    | 'empty_output'
    | 'error_in_data'
    | 'partial_failure'
    | 'truncated_output'
    | 'json_in_string'
    | 'confidence_mismatch'
    | 'retrieval_quality_low'
    | 'shallow_context'
    | 'shallow_output'
    | 'information_compression_anomaly'
    | 'placeholder_detected'
    | 'semantic_degradation'
    | 'structural_anomaly'
    | 'selective_attention_reduction'
    | 'input_echo'
    | 'semantic_contradiction'
    | 'context_size_anomaly'
  field_name: string
  severity: 'critical' | 'warning'
  evidence: string
}

export interface FieldMismatch {
  field_name: string
  expected_type: string
  actual_type: string
  actual_value_repr: string
}

export interface InspectionResult {
  is_silent_failure: boolean
  missing_fields: string[]
  empty_fields: string[]
  type_mismatches: FieldMismatch[]
  severity: Severity
  message: string
  unannotated_successors: string[]
  suspicious_empty_keys: string[]
  tool_failures: ToolFailure[]
  has_tool_failure: boolean
  semantic_signals: SemanticSignal[]
  degraded_fields: string[]
  degraded_upstream_node: string | null
}

export interface ValidatorResult {
  validator_name: string
  is_valid: boolean
  message: string
}

export interface LLMCallInfo {
  model_name: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_usd: number | null
}

export interface LLMUsage {
  calls: LLMCallInfo[]
  total_prompt_tokens: number
  total_completion_tokens: number
  total_tokens: number
  total_cost_usd: number | null
}

export interface SemanticCheckResult {
  passed: boolean
  reason: string
  confidence: number
  model: string
  prompt_tokens: number
  completion_tokens: number
  duration_ms: number
  evidence_considered?: string[]
  overridden_signals?: string[]
}

export interface NodeEvent {
  step_index: number
  node_name: string
  status: StepStatus
  input_state: Record<string, unknown> | null
  output_dict: Record<string, unknown> | null
  duration_ms: number
  timestamp_utc: string
  exception: string | null
  inspection: InspectionResult | null
  attempt_index: number
  total_iterations?: number | null
  validator_results: ValidatorResult[]
  is_subgraph_entry: boolean
  subgraph_run_id: string | null
  llm_usage?: LLMUsage | null
  behavior_type?: BehaviorType | null
  anomaly_signals?: AnomalySignal[]
  semantic_check?: SemanticCheckResult | null
}

export interface PropagationLink {
  source_node: string
  target_node: string
  signal_type: 'field_drop' | 'placeholder' | 'anomaly_cascade' | 'semantic_collapse'
  confidence: number
  evidence: string
}

export interface DegradationOrigin {
  node_name: string
  step_index: number
  signal_types: string[]
  confidence: number
  reason: string
}

export interface PropagationChain {
  chain_type:
    | 'tool_failure_cascade'
    | 'semantic_collapse'
    | 'field_drop_cascade'
    | 'placeholder_propagation'
    | 'anomaly_cascade'
    | 'mixed_degradation'
  nodes: string[]
  links: PropagationLink[]
  summary: string
}

export interface TimelineEvent {
  step_index: number
  node_name: string
  event_type: 'node_ok' | 'degradation_onset' | 'propagation' | 'crash'
  label: string
  signal_summary: string
}

export interface ReplayImpact {
  improved_nodes: string[]
  regressed_nodes: string[]
  key_fix_node: string | null
  downstream_improvement_count: number
  summary: string
}

export interface CorrelationReport {
  run_id: string
  degradation_origins: DegradationOrigin[]
  propagation_chains: PropagationChain[]
  causal_summary: string
  timeline: TimelineEvent[]
  replay_impact: ReplayImpact | null
}

export interface SemanticHypothesis {
  hypothesis: string
  confidence: number
  supporting_evidence: string[]
  category: string
}

export interface SuggestedSignature {
  pattern: string
  match_strategy: string
  proposed_category: string
  severity: 'critical' | 'warning'
  description: string
  evidence: string[]
  confidence: number
  reasoning: string
}

export interface LLMInvestigationResult {
  triggered: boolean
  trigger_reasons: string[]
  root_cause_explanation: string
  causal_hypotheses: SemanticHypothesis[]
  degradation_narrative: string
  observations: string[]
  debugging_suggestions: string[]
  confidence: number
  suggested_signatures: SuggestedSignature[]
  model_used: string
  prompt_tokens: number
  completion_tokens: number
  investigation_duration_ms: number
  error?: string | null
}

export interface NodeDiffSummary {
  node_name: string
  status_before: string
  status_after: string
  summary: string
  verdict: 'fixed' | 'regressed' | 'unchanged' | 'changed'
}

export interface ReplayComparisonResult {
  structural_summary: string
  failure_analysis: string
  root_cause_delta: string
  key_insights: string[]
  recommendation: string
  confidence: number
  node_summaries: NodeDiffSummary[]
  model_used: string
  prompt_tokens: number
  completion_tokens: number
  duration_ms: number
  error?: string | null
}

export type FindingSource = 'heuristic' | 'validator' | 'anomaly' | 'llm' | 'crash'

/** One normalized failure signal on a run — mirrors argus.models.Finding. */
export interface Finding {
  id: string
  node: string
  type: string
  severity: 'critical' | 'warning' | 'info'
  reason: string
  source: FindingSource
  field_path?: string | null
  origin_node?: string | null
  confidence?: number | null
  suppressed?: boolean
}

export interface ToolChainFinding {
  finding_id: string
  finding_type: 'retry_storm' | 'ordering_anomaly' | 'unused_result' | 'argument_degradation'
  severity: 'critical' | 'warning'
  nodes_involved: string[]
  description: string
  evidence: string
  confidence: number
}

export interface LoopIterationDiff {
  from_attempt: number
  to_attempt: number
  summary: string
  fields_changed: string[]
}

export interface LoopAnalysisResult {
  node_name: string
  total_iterations: number
  summary: string
  is_stalled: boolean
  stall_details: string | null
  unnecessary_retries: number
  unnecessary_details: string | null
  iteration_diffs: LoopIterationDiff[]
  model_used: string
  prompt_tokens: number
  completion_tokens: number
  duration_ms: number
  error: string | null
}

export interface RunRecord {
  run_id: string
  argus_version: string
  started_at: string
  completed_at: string | null
  duration_ms: number | null
  overall_status: RunStatus
  first_failure_step: string | null
  root_cause_chain: string[]
  graph_node_names: string[]
  graph_edge_map: Record<string, string[]>
  initial_state: Record<string, unknown>
  steps: NodeEvent[]
  parent_run_id: string | null
  replay_from_step: string | null
  is_cyclic: boolean
  subgraph_run_ids: string[]
  interrupted: boolean
  interrupt_node: string | null
  total_llm_calls?: number
  total_tokens?: number
  total_cost_usd?: number | null
  behavior_config?: BehaviorConfig | null
  correlation?: CorrelationReport | null
  llm_investigation?: LLMInvestigationResult | null
  replay_comparison?: ReplayComparisonResult | null
  loop_analyses?: LoopAnalysisResult[] | null
  tool_chain_findings?: ToolChainFinding[] | null
  findings?: Finding[]
  node_fn_refs?: Record<string, string> | null
  node_fn_paths?: Record<string, string> | null
  dry_run?: boolean
  schema_version?: string
  state_patch?: Record<string, unknown> | null
  coverage_summary?: Record<string, number>
  app_factory_ref?: string | null
}

export interface RunSummary {
  run_id: string
  overall_status: RunStatus
  started_at: string
  duration_ms: number | null
  step_count: number
  first_failure_step: string | null
  graph_node_names: string[]
  argus_version: string
  parent_run_id: string | null
  replay_from_step?: string | null
  alias?: string | null
  origins?: string[]
  finding_nodes?: string[]
}
