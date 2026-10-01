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
  // A node that returned a literal `{}` contributed nothing to state — a
  // contract failure between nodes, not a tool that misbehaved.
  empty_output:                  { label: 'Empty Output',        category: 'Coherence', categoryColor: 'var(--coherence)' },
  // Latency — timing-correlated degradation (VAR-8)
  timeout_adjacent:              { label: 'Near Timeout',        category: 'Quality',   categoryColor: 'var(--quality)' },
  suspiciously_fast:             { label: 'Suspiciously Fast',   category: 'Quality',   categoryColor: 'var(--quality)' },
  latency_quality_mismatch:      { label: 'Fast + Failed',       category: 'Quality',   categoryColor: 'var(--quality)' },
}

const FALLBACK: FailureMeta = { label: 'Unknown', category: 'Tool', categoryColor: 'var(--idle)' }

export function getFailureMeta(failureType: string): FailureMeta {
  return FAILURE_META[failureType] ?? FALLBACK
}

/* ── Findings → category capsule ──────────────────────────────────
   A Finding's `type` is a failure_type, a signature category, a tool-chain
   finding, an anomaly id or `crash`. Each lands in one of the four signal
   families so the UI can colour it: Tool red, Quality amber, Semantic
   violet, Coherence cyan. */

export const CATEGORY_CHIP: Record<FailureMeta['category'], string> = {
  Tool: 'chip-tool', Quality: 'chip-quality', Semantic: 'chip-semantic', Coherence: 'chip-coherence',
}

const SIGNATURE_LABEL: Record<string, string> = {
  placeholder_outputs: 'Placeholder', null_like_semantic: 'Null-like value',
  suspicious_phrases: 'Suspicious phrase', corrupted_markers: 'Corrupted marker',
  repeated_filler: 'Repeated filler', malformed_payload: 'Malformed payload',
  empty_semantic_state: 'Empty state', semantic_refusal: 'Refusal',
}
const CHAIN_LABEL: Record<string, string> = {
  unused_result: 'Unused result', retry_storm: 'Retry storm',
  ordering_anomaly: 'Ordering anomaly', argument_degradation: 'Argument degradation',
}

function humanize(s: string): string {
  const t = s.replace(/_/g, ' ').trim()
  return t ? t[0].toUpperCase() + t.slice(1) : 'Signal'
}

export function findingMeta(f: { type: string; source?: string; severity?: string }): FailureMeta & { chip: string } {
  const pick = (category: FailureMeta['category'], label: string) =>
    ({ label, category, categoryColor: `var(--${category === 'Tool' ? 'tool' : category.toLowerCase()})`, chip: CATEGORY_CHIP[category] })
  if (f.source === 'crash' || f.type === 'crash') return pick('Tool', 'Crash')
  if (FAILURE_META[f.type]) { const m = FAILURE_META[f.type]; return { ...m, chip: CATEGORY_CHIP[m.category] } }
  if (SIGNATURE_LABEL[f.type]) return pick('Semantic', SIGNATURE_LABEL[f.type])
  if (CHAIN_LABEL[f.type]) return pick('Coherence', CHAIN_LABEL[f.type])
  if (f.source === 'anomaly') return pick('Quality', `Anomaly ${f.type}`)
  if (f.source === 'llm') return pick('Semantic', 'Judge')
  if (f.source === 'validator') return pick('Semantic', 'Validator')
  if (/missing/.test(f.type)) return pick('Tool', 'Missing field')
  return pick(f.severity === 'critical' ? 'Tool' : 'Quality', humanize(f.type))
}
