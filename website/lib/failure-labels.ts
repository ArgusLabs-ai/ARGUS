/** Maps each failure_type to a human-readable label and category for UI rendering. */

export interface FailureMeta {
  label: string
  category: 'Tool' | 'Quality' | 'Semantic' | 'Coherence'
  categoryColor: string
}

export const FAILURE_META: Record<string, FailureMeta> = {
  // Tool — hard errors from external calls
  error_response:     { label: 'Error Response',     category: 'Tool',      categoryColor: 'var(--tool)' },
  rate_limit:         { label: 'Rate Limited',        category: 'Tool',      categoryColor: 'var(--tool)' },
  empty_result:       { label: 'Empty Result',        category: 'Tool',      categoryColor: 'var(--tool)' },
  empty_output:       { label: 'Empty Output',        category: 'Tool',      categoryColor: 'var(--tool)' },
  error_in_data:      { label: 'Error in Data',       category: 'Tool',      categoryColor: 'var(--tool)' },
  partial_failure:    { label: 'Partial Failure',     category: 'Tool',      categoryColor: 'var(--tool)' },
  // Quality — output exists but is degraded
  truncated_output:                { label: 'Truncated',          category: 'Quality',   categoryColor: 'var(--quality)' },
  json_in_string:                  { label: 'Double-Encoded JSON', category: 'Quality',  categoryColor: 'var(--quality)' },
  confidence_mismatch:             { label: 'Confidence Mismatch', category: 'Quality',  categoryColor: 'var(--quality)' },
  retrieval_quality_low:           { label: 'Low Retrieval',      category: 'Quality',   categoryColor: 'var(--quality)' },
  shallow_context:                 { label: 'Shallow Context',    category: 'Quality',   categoryColor: 'var(--quality)' },
  shallow_output:                  { label: 'Shallow Output',     category: 'Quality',   categoryColor: 'var(--quality)' },
  information_compression_anomaly: { label: 'Over-Compressed',    category: 'Quality',   categoryColor: 'var(--quality)' },
  // Semantic — LLM output smells
  placeholder_detected: { label: 'Placeholder',      category: 'Semantic',  categoryColor: 'var(--semantic)' },
  semantic_degradation: { label: 'Degradation',      category: 'Semantic',  categoryColor: 'var(--semantic)' },
  structural_anomaly:   { label: 'Structural',       category: 'Semantic',  categoryColor: 'var(--semantic)' },
  // Coherence — input-output relationship issues (VAR-7)
  selective_attention_reduction: { label: 'Selective Attention', category: 'Coherence', categoryColor: 'var(--coherence)' },
  input_echo:                    { label: 'Input Echo',          category: 'Coherence', categoryColor: 'var(--coherence)' },
  semantic_contradiction:        { label: 'Contradiction',       category: 'Coherence', categoryColor: 'var(--coherence)' },
  context_size_anomaly:          { label: 'Context Overflow',    category: 'Coherence', categoryColor: 'var(--coherence)' },
  // Latency — timing-correlated degradation (VAR-8)
  timeout_adjacent:              { label: 'Near Timeout',        category: 'Quality',   categoryColor: 'var(--quality)' },
  suspiciously_fast:             { label: 'Suspiciously Fast',   category: 'Quality',   categoryColor: 'var(--quality)' },
  latency_quality_mismatch:      { label: 'Fast + Failed',       category: 'Quality',   categoryColor: 'var(--quality)' },
}

const FALLBACK: FailureMeta = { label: 'Unknown', category: 'Tool', categoryColor: 'var(--idle)' }

export function getFailureMeta(failureType: string): FailureMeta {
  return FAILURE_META[failureType] ?? FALLBACK
}
