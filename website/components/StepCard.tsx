'use client'

import { useState } from 'react'
import type { NodeEvent } from '@/lib/types'
import { StepStatusBadge, SeverityBadge } from './StatusBadge'
import { getFailureMeta } from '@/lib/failure-labels'
import JsonViewer from './JsonViewer'

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

interface StepCardProps {
  step: NodeEvent
  defaultOpen?: boolean
  isBreakpoint?: boolean
  isDimmed?: boolean
}

export default function StepCard({ step, defaultOpen = false, isBreakpoint = false, isDimmed = false }: StepCardProps) {
  const [open, setOpen] = useState(defaultOpen)

  const hasIssues = step.status !== 'pass' || (step.inspection && step.inspection.severity !== 'ok')
  const isPass = step.status === 'pass' && !hasIssues

  const toolFailures = step.inspection?.tool_failures ?? []
  const semanticSignals = step.inspection?.semantic_signals ?? []
  const issueCount = toolFailures.length + semanticSignals.length +
    (step.inspection?.missing_fields?.length ?? 0)

  const borderColor = isBreakpoint
    ? 'var(--tool)'
    : hasIssues
    ? 'color-mix(in srgb, var(--tool) 45%, transparent)'
    : 'var(--border-default)'

  const bgColor = isBreakpoint
    ? 'var(--tool-dim)'
    : 'var(--bg-surface)'

  return (
    <div className={isDimmed ? 'opacity-45 saturate-50' : undefined}>
      <div
        className="rounded-lg relative overflow-hidden"
        style={{
          border: `1px solid ${borderColor}`,
          background: bgColor,
          boxShadow: isBreakpoint ? '0 0 28px var(--tool-dim)' : 'var(--shadow-md)',
        }}
      >
        {/* Breakpoint accent */}
        {isBreakpoint && (
          <div className="absolute left-0 top-0 bottom-0 w-1 bg-red-500/80" />
        )}

        {/* Header */}
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-white/[0.03] transition-colors"
        >
          <span className={`font-mono text-xs w-5 shrink-0 tabular-nums ${isBreakpoint ? 'text-red-400' : 'text-[var(--line-3)]'}`}>
            {step.step_index}
          </span>

          <span className="text-[var(--ink-4)] text-xs shrink-0">{open ? '▾' : '▸'}</span>

          <span className={`text-sm font-mono flex-1 truncate ${isPass ? 'text-[var(--text-muted)]' : 'text-[var(--text-primary)]'}`}>
            {step.node_name}
            {step.is_subgraph_entry && (
              <span className="ml-2 text-[10px] text-[var(--ink-4)] border border-[var(--border-subtle)] px-1 rounded">
                ↳ sub
              </span>
            )}
          </span>

          {/* Inline issue count */}
          {issueCount > 0 && (
            <span
              className="text-[10px] font-bold px-1.5 py-0.5 rounded-full tabular-nums"
              style={{
                color: toolFailures.some((f) => f.severity === 'critical') ? 'var(--tool)' : 'var(--quality)',
                background: toolFailures.some((f) => f.severity === 'critical')
                  ? 'var(--tool-dim)' : 'var(--quality-dim)',
              }}
            >
              {issueCount}
            </span>
          )}

          <StepStatusBadge status={step.status} />

          <span className="font-mono text-[var(--text-secondary)] text-xs w-14 text-right shrink-0">
            {formatDuration(step.duration_ms)}
          </span>
        </button>

        {/* Expanded */}
        {open && (
          <div
            className="px-4 py-4 space-y-5"
            style={{ borderTop: '1px solid var(--border-default)', background: 'var(--bg-elevated)' }}
          >
            {step.attempt_index > 0 && (
              <div className="text-xs text-[var(--ink-3)] font-mono border border-[var(--border-subtle)] rounded px-2 py-1 w-fit">
                attempt #{step.attempt_index}
              </div>
            )}

            {/* Exception */}
            {step.exception && (
              <div>
                <div className="text-[10px] uppercase tracking-widest font-semibold text-red-400 mb-2">Exception</div>
                <pre className="text-xs text-red-300/80 bg-red-950/15 border border-red-900/25 rounded-lg p-3.5 overflow-x-auto whitespace-pre-wrap leading-5 font-mono">
                  {step.exception}
                </pre>
              </div>
            )}

            {/* Inspection issues */}
            {step.inspection && step.inspection.severity !== 'ok' && (
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <div className="text-[10px] uppercase tracking-widest font-semibold text-[var(--ink-4)]">Inspection</div>
                  <SeverityBadge severity={step.inspection.severity} />
                </div>

                {step.inspection.missing_fields.length > 0 && (
                  <div className="flex items-center gap-2 flex-wrap text-xs">
                    <span className="text-red-400 font-medium">missing:</span>
                    {step.inspection.missing_fields.map((f) => (
                      <code key={f} className="rounded px-1.5 py-0.5 font-mono text-[11px]"
                        style={{ background: 'var(--tool-dim)', color: 'var(--tool)' }}>
                        {f}
                      </code>
                    ))}
                  </div>
                )}

                {step.inspection.empty_fields.length > 0 && (
                  <div className="text-xs">
                    <span className="text-[var(--ink-4)]">empty optional: </span>
                    <span className="text-[var(--ink-3)] font-mono">{step.inspection.empty_fields.join(', ')}</span>
                  </div>
                )}

                {step.inspection.type_mismatches.length > 0 && (
                  <div className="space-y-1">
                    {step.inspection.type_mismatches.map((m, i) => (
                      <div key={i} className="text-xs font-mono pl-2">
                        <span className="text-blue-400">{m.field_name}</span>
                        <span className="text-[var(--ink-4)]"> expected </span>
                        <span className="text-green-400">{m.expected_type}</span>
                        <span className="text-[var(--ink-4)]"> got </span>
                        <span className="text-amber-400">{m.actual_type}</span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Tool failures — with labels and category pills */}
                {toolFailures.length > 0 && (
                  <div className="space-y-1.5">
                    {toolFailures.map((tf, i) => {
                      const meta = getFailureMeta(tf.failure_type)
                      const sevColor = tf.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'
                      return (
                        <div
                          key={i}
                          className="text-xs rounded-lg px-3 py-2"
                          style={{
                            background: `color-mix(in srgb, ${sevColor} 4%, transparent)`,
                            border: `1px solid color-mix(in srgb, ${sevColor} 12%, transparent)`,
                          }}
                        >
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="size-1.5 rounded-full" style={{ background: sevColor }} />
                            <span
                              className="text-[10px] font-semibold px-1.5 py-px rounded"
                              style={{
                                color: meta.categoryColor,
                                background: `color-mix(in srgb, ${meta.categoryColor} 10%, transparent)`,
                                border: `1px solid color-mix(in srgb, ${meta.categoryColor} 20%, transparent)`,
                              }}
                            >
                              {meta.category}
                            </span>
                            <span className="font-medium" style={{ color: sevColor }}>
                              {meta.label}
                            </span>
                            <code className="text-[11px] font-mono text-[var(--ink-3)]">{tf.field_name}</code>
                          </div>
                          <div className="text-[var(--ink-3)] mt-0.5 font-mono text-[11px]">{tf.evidence}</div>
                        </div>
                      )
                    })}
                  </div>
                )}

                {/* Semantic signals */}
                {semanticSignals.length > 0 && (
                  <div className="space-y-1.5">
                    <div className="text-[10px] uppercase tracking-widest font-semibold text-[var(--ink-4)]">Semantic Signals</div>
                    {semanticSignals.map((sig, i) => {
                      const sevColor = sig.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'
                      return (
                        <div
                          key={i}
                          className="text-xs rounded-lg px-3 py-2"
                          style={{
                            background: 'color-mix(in srgb, var(--semantic) 4%, transparent)',
                            border: '1px solid color-mix(in srgb, var(--semantic) 12%, transparent)',
                          }}
                        >
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="size-1.5 rounded-full" style={{ background: sevColor }} />
                            <code className="text-[10px] font-mono font-bold text-[var(--ink-3)]">{sig.sig_id}</code>
                            <span className="font-medium text-[var(--ink)]">{sig.description}</span>
                          </div>
                          {sig.evidence && (
                            <div className="text-[var(--ink-3)] mt-0.5 font-mono text-[11px]">{sig.evidence}</div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )}

            {/* Validators */}
            {step.validator_results.length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-widest font-semibold text-[var(--ink-4)] mb-2">Validators</div>
                <div className="space-y-1">
                  {step.validator_results.map((v, i) => (
                    <div key={i} className="flex items-start gap-2.5 text-xs">
                      <span className={v.is_valid ? 'text-green-400' : 'text-red-400'}>{v.is_valid ? '✓' : '✗'}</span>
                      <span className="text-[var(--ink-4)]">{v.validator_name}</span>
                      {v.message && <span className="text-[var(--ink-3)]">{v.message}</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Input / Output */}
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {step.input_state !== null && (
                <div>
                  <div className="text-[10px] uppercase tracking-widest font-semibold text-[var(--ink-4)] mb-2">Input</div>
                  <JsonViewer data={step.input_state} defaultCollapsed={true} />
                </div>
              )}
              {step.output_dict !== null && (
                <div>
                  <div className="text-[10px] uppercase tracking-widest font-semibold text-[var(--ink-4)] mb-2">Output</div>
                  <JsonViewer data={step.output_dict} defaultCollapsed={true} />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
