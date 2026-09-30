'use client'

import { Brain } from 'lucide-react'
import type { NodeEvent, RunRecord, SemanticSignal, ToolFailure } from '@/lib/types'
import { getFailureMeta } from '@/lib/failure-labels'

/* Signal presentation for the step inspector: the decision narrative plus the
   per-tool-failure and per-semantic-signal rows. Split out of StepInspector.tsx,
   which had grown to 795 lines. */
function CategoryPill({ category, color }: { category: string; color: string }) {
  return (
    <span
      className="text-[10px] font-semibold px-1.5 py-px rounded"
      style={{
        color,
        background: `color-mix(in srgb, ${color} 10%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 20%, transparent)`,
      }}
    >
      {category}
    </span>
  )
}


function deriveFixHint(step: NodeEvent, run: RunRecord): string | null {
  const inv = run.llm_investigation
  if (inv?.triggered && inv.debugging_suggestions?.length) return inv.debugging_suggestions[0].split('\n')[0]
  if (run.correlation?.causal_summary) return run.correlation.causal_summary
  if (step.inspection?.degraded_upstream_node) return `Check upstream node "${step.inspection.degraded_upstream_node}" — it failed to produce required fields`
  return null
}

export function DecisionExplanation({ step, run }: { step: NodeEvent; run: RunRecord }) {
  const status = step.status
  if (status === 'pass' || status === 'interrupted' || status === 'skipped' || status === 'retried') return null

  const insp = step.inspection
  const sc = step.semantic_check
  const anomalies = step.anomaly_signals ?? []
  const failedValidators = step.validator_results.filter((v) => !v.is_valid)
  const toolFailures = insp?.tool_failures ?? []
  const semanticSignals = insp?.semantic_signals ?? []

  // Don't render if there's nothing to explain
  const hasContent = sc || anomalies.length > 0 || failedValidators.length > 0 || toolFailures.length > 0 || semanticSignals.length > 0
  if (!hasContent) return null

  const accentColor = status === 'semantic_fail' ? 'var(--semantic)'
    : status === 'crashed' ? 'var(--tool)'
    : status === 'degraded_input' ? 'var(--quality)'
    : 'var(--quality)'

  const fixHint = deriveFixHint(step, run)

  return (
    <div
      className="mt-4 rounded-xl overflow-hidden"
      style={{
        background: `color-mix(in srgb, ${accentColor} 3%, var(--card))`,
        border: `1px solid color-mix(in srgb, ${accentColor} 18%, transparent)`,
      }}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-2.5" style={{ borderBottom: `1px solid color-mix(in srgb, ${accentColor} 10%, transparent)` }}>
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <circle cx="8" cy="8" r="7" stroke={accentColor} strokeWidth="1.5" fill="none" opacity="0.5" />
          <text x="8" y="12" textAnchor="middle" fill={accentColor} fontSize="11" fontWeight="bold">?</text>
        </svg>
        <span className="text-[13px] font-bold" style={{ color: accentColor, letterSpacing: '-0.01em' }}>
          Why This Was Flagged
        </span>
      </div>

      <div className="px-4 py-3 flex flex-col gap-3">

        {/* Rules Triggered — validators + tool failures + semantic signals */}
        {(failedValidators.length > 0 || toolFailures.length > 0 || semanticSignals.length > 0) && (
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">Rules Triggered</p>
            <div className="flex flex-col gap-1">
              {failedValidators.map((v, i) => (
                <div key={`v-${i}`} className="flex items-start gap-2 text-[12.5px]">
                  <span className="mt-0.5 shrink-0 text-[11px]" style={{ color: 'var(--semantic)' }}>⊗</span>
                  <div className="min-w-0">
                    <span className="font-mono font-semibold text-foreground text-[12px]">{v.validator_name}</span>
                    {v.message && <p className="text-muted-foreground text-[11.5px] mt-0.5 leading-relaxed">{v.message}</p>}
                  </div>
                </div>
              ))}
              {toolFailures.filter((f) => f.severity === 'critical').map((tf, i) => {
                const meta = getFailureMeta(tf.failure_type)
                return (
                  <div key={`tf-${i}`} className="flex items-start gap-2 text-[12.5px]">
                    <span className="mt-0.5 shrink-0 text-[11px]" style={{ color: 'var(--tool)' }}>⚠</span>
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <CategoryPill category={meta.category} color={meta.categoryColor} />
                        <span className="font-medium text-foreground text-[12px]">{meta.label}</span>
                        <code className="text-[10px] font-mono text-muted-foreground">{tf.field_name}</code>
                      </div>
                      <p className="text-muted-foreground text-[11.5px] mt-0.5 leading-relaxed">{tf.evidence}</p>
                    </div>
                  </div>
                )
              })}
              {semanticSignals.map((sig, i) => (
                <div key={`sig-${i}`} className="flex items-start gap-2 text-[12.5px]">
                  <span className="mt-0.5 shrink-0 text-[11px]" style={{ color: 'var(--semantic)' }}>⊗</span>
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <code className="text-[10px] font-mono font-bold text-muted-foreground">{sig.sig_id}</code>
                      <span className="text-foreground text-[12px]">{sig.description}</span>
                    </div>
                    {sig.evidence && <p className="text-muted-foreground text-[11px] mt-0.5 font-mono">{sig.evidence}</p>}
                    {sig.field_path.length > 0 && (
                      <p className="text-muted-foreground/60 text-[10.5px] mt-0.5 font-mono">{sig.field_path.join('.')}</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Semantic Judge */}
        {sc && (
          <div
            className="rounded-lg px-3 py-2.5"
            style={{
              background: `color-mix(in srgb, ${sc.passed ? 'var(--ok)' : 'var(--semantic)'} 5%, transparent)`,
              border: `1px solid color-mix(in srgb, ${sc.passed ? 'var(--ok)' : 'var(--semantic)'} 14%, transparent)`,
            }}
          >
            <div className="flex items-center gap-2 mb-1">
              <Brain className="size-3" style={{ color: sc.passed ? 'var(--ok)' : 'var(--semantic)' }} />
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Semantic Judge</span>
              <span className="text-[11px] font-mono font-semibold" style={{ color: sc.passed ? 'var(--ok)' : 'var(--semantic)' }}>
                {sc.passed ? '✓ Coherent' : '✗ Incoherent'} {Math.round(sc.confidence * 100)}%
              </span>
            </div>
            {sc.reason && (
              <p className="text-[12px] text-muted-foreground leading-relaxed">{sc.reason}</p>
            )}
            {(sc.evidence_considered?.length ?? 0) > 0 && (
              <div className="mt-2">
                <p className="text-[10px] text-muted-foreground/60 uppercase tracking-wide mb-1">Evidence Considered</p>
                {sc.evidence_considered!.map((e, i) => (
                  <p key={i} className="text-[11px] text-muted-foreground pl-2 border-l-2 border-border leading-relaxed">{e}</p>
                ))}
              </div>
            )}
            {(sc.overridden_signals?.length ?? 0) > 0 && (
              <div className="mt-2">
                <p className="text-[10px] text-[var(--quality)] uppercase tracking-wide mb-1">Overridden Signals</p>
                {sc.overridden_signals!.map((s, i) => (
                  <p key={i} className="text-[11px] text-[var(--quality)] pl-2 border-l-2 border-[var(--quality)]/30 leading-relaxed">{s}</p>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Behavioral Anomalies */}
        {anomalies.length > 0 && (
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">Behavioral Anomalies</p>
            <div className="flex flex-col gap-1.5">
              {anomalies.map((a, i) => {
                const sevColor = a.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'
                return (
                  <div
                    key={i}
                    className="rounded-lg px-3 py-2"
                    style={{
                      background: `color-mix(in srgb, ${sevColor} 4%, transparent)`,
                      border: `1px solid color-mix(in srgb, ${sevColor} 12%, transparent)`,
                    }}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="size-1.5 rounded-full" style={{ background: sevColor }} />
                      <span className="text-[11px] font-semibold" style={{ color: sevColor }}>{a.severity}</span>
                      <span className="font-mono text-[10px] text-muted-foreground">suspicion {(a.suspicion_score * 100).toFixed(0)}%</span>
                    </div>
                    <p className="text-[12.5px] text-foreground/90 leading-relaxed">{a.reason}</p>
                    {(a.expected_behavior || a.observed_behavior) && (
                      <div className="mt-1.5 grid grid-cols-2 gap-3 text-[11px]">
                        {a.expected_behavior && (
                          <div>
                            <span className="text-muted-foreground/60 uppercase text-[10px] tracking-wider">Expected</span>
                            <p className="text-muted-foreground mt-0.5">{a.expected_behavior}</p>
                          </div>
                        )}
                        {a.observed_behavior && (
                          <div>
                            <span className="text-muted-foreground/60 uppercase text-[10px] tracking-wider">Observed</span>
                            <p className="text-muted-foreground mt-0.5">{a.observed_behavior}</p>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Fix Hint */}
        {fixHint && (
          <div className="flex items-start gap-2 rounded-lg px-3 py-2" style={{ background: 'var(--iris-dim)', border: '1px solid var(--iris-dim)' }}>
            <span className="text-[12px] mt-0.5 shrink-0" style={{ color: 'var(--iris-bright)' }}>⚑</span>
            <p className="text-[12px] leading-relaxed" style={{ color: 'var(--iris-bright)' }}>{fixHint}</p>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Issue Row ──────────────────────────────────────────────────── */

export function ToolFailureRow({ tf }: { tf: ToolFailure }) {
  const meta = getFailureMeta(tf.failure_type)
  const sevColor = tf.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'

  return (
    <div
      className="flex items-start gap-2.5 px-3 py-2 rounded-lg"
      style={{
        background: `color-mix(in srgb, ${sevColor} 4%, transparent)`,
        border: `1px solid color-mix(in srgb, ${sevColor} 12%, transparent)`,
      }}
    >
      {/* severity dot */}
      <span className="mt-1.5 shrink-0 size-1.5 rounded-full" style={{ background: sevColor }} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <CategoryPill category={meta.category} color={meta.categoryColor} />
          <span className="text-[13px] font-medium text-foreground">{meta.label}</span>
          <code className="text-[11px] font-mono text-muted-foreground">{tf.field_name}</code>
        </div>
        <p className="text-[12px] text-muted-foreground mt-0.5 leading-relaxed">{tf.evidence}</p>
      </div>
    </div>
  )
}

export function SemanticSignalRow({ sig }: { sig: SemanticSignal }) {
  const sevColor = sig.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'
  return (
    <div
      className="flex items-start gap-2.5 px-3 py-2 rounded-lg"
      style={{
        background: 'color-mix(in srgb, var(--semantic) 4%, transparent)',
        border: '1px solid color-mix(in srgb, var(--semantic) 12%, transparent)',
      }}
    >
      <span className="mt-1.5 shrink-0 size-1.5 rounded-full" style={{ background: sevColor }} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <code className="text-[10px] font-mono font-bold text-muted-foreground">{sig.sig_id}</code>
          <span className="text-[13px] font-medium text-foreground">{sig.description}</span>
        </div>
        <div className="flex items-center gap-2 mt-0.5">
          <span className="text-[11px] text-muted-foreground/70">{sig.category}</span>
          {sig.field_path.length > 0 && (
            <code className="text-[11px] font-mono text-muted-foreground">{sig.field_path.join('.')}</code>
          )}
        </div>
        {sig.evidence && (
          <p className="text-[11.5px] text-muted-foreground mt-0.5 font-mono">{sig.evidence}</p>
        )}
      </div>
    </div>
  )
}

/* ── Main Component ─────────────────────────────────────────────── */

