/** Maps each failure_type to a human-readable label and category for UI rendering. */

export interface FailureMeta {
  label: string
  category: 'Tool' | 'Quality' | 'Semantic' | 'Coherence'
  categoryColor: string
}

export const FAILURE_META: Record<string, FailureMeta> = {
  // Tool — hard errors from external calls
  error_response:     { label: 'Error Response',     category: 'Tool',      categoryColor: '#ef4444' },
  rate_limit:         { label: 'Rate Limited',        category: 'Tool',      categoryColor: '#ef4444' },
  empty_result:       { label: 'Empty Result',        category: 'Tool',      categoryColor: '#ef4444' },
  empty_output:       { label: 'Empty Output',        category: 'Tool',      categoryColor: '#ef4444' },
  error_in_data:      { label: 'Error in Data',       category: 'Tool',      categoryColor: '#ef4444' },
  partial_failure:    { label: 'Partial Failure',     category: 'Tool',      categoryColor: '#ef4444' },
  // Quality — output exists but is degraded
  truncated_output:                { label: 'Truncated',          category: 'Quality',   categoryColor: '#f59e0b' },
  json_in_string:                  { label: 'Double-Encoded JSON', category: 'Quality',  categoryColor: '#f59e0b' },
  confidence_mismatch:             { label: 'Confidence Mismatch', category: 'Quality',  categoryColor: '#f59e0b' },
  retrieval_quality_low:           { label: 'Low Retrieval',      category: 'Quality',   categoryColor: '#f59e0b' },
  shallow_context:                 { label: 'Shallow Context',    category: 'Quality',   categoryColor: '#f59e0b' },
  shallow_output:                  { label: 'Shallow Output',     category: 'Quality',   categoryColor: '#f59e0b' },
  information_compression_anomaly: { label: 'Over-Compressed',    category: 'Quality',   categoryColor: '#f59e0b' },
  // Semantic — LLM output smells
  placeholder_detected: { label: 'Placeholder',      category: 'Semantic',  categoryColor: '#a855f7' },
  semantic_degradation: { label: 'Degradation',      category: 'Semantic',  categoryColor: '#a855f7' },
  structural_anomaly:   { label: 'Structural',       category: 'Semantic',  categoryColor: '#a855f7' },
  // Coherence — input-output relationship issues (VAR-7)
  selective_attention_reduction: { label: 'Selective Attention', category: 'Coherence', categoryColor: '#6366f1' },
  input_echo:                    { label: 'Input Echo',          category: 'Coherence', categoryColor: '#6366f1' },
  semantic_contradiction:        { label: 'Contradiction',       category: 'Coherence', categoryColor: '#6366f1' },
  context_size_anomaly:          { label: 'Context Overflow',    category: 'Coherence', categoryColor: '#6366f1' },
  // Latency — timing-correlated degradation (VAR-8)
  timeout_adjacent:              { label: 'Near Timeout',        category: 'Quality',   categoryColor: '#f59e0b' },
  suspiciously_fast:             { label: 'Suspiciously Fast',   category: 'Quality',   categoryColor: '#f59e0b' },
  latency_quality_mismatch:      { label: 'Fast + Failed',       category: 'Quality',   categoryColor: '#f59e0b' },
}

const FALLBACK: FailureMeta = { label: 'Unknown', category: 'Tool', categoryColor: '#6b7280' }

export function getFailureMeta(failureType: string): FailureMeta {
  return FAILURE_META[failureType] ?? FALLBACK
}
