'use client'

import { useState } from 'react'
import {
  Info,
  X as XIcon,
  ChevronRight,
  ChevronDown,
  Check,
  AlertTriangle,
  Brain,
  ShieldCheck,
  Bug,
  Activity,
  ArrowRight,
  Layers,
} from 'lucide-react'
import type { RunRecord, NodeEvent, ToolFailure, SemanticSignal, AnomalySignal } from '@/lib/types'
import { formatDur } from '@/lib/run-utils'
import { getFailureMeta } from '@/lib/failure-labels'
import JsonViewer from '@/components/JsonViewer'
import { DecisionExplanation, ToolFailureRow, SemanticSignalRow } from '@/components/run-detail/StepInspectorSignals'

/* ── Helpers ────────────────────────────────────────────────────── */

function statusBadge(status: string) {
  const map: Record<string, { label: string; color: string; bg: string }> = {
    pass:           { label: 'Passed',        color: 'var(--ok)', bg: 'var(--ok-dim)' },
    crashed:        { label: 'Crashed',       color: 'var(--tool)', bg: 'var(--tool-dim)' },
    fail:           { label: 'Failed',        color: 'var(--tool)', bg: 'var(--tool-dim)' },
    semantic_fail:  { label: 'Semantic Fail', color: 'var(--semantic)', bg: 'var(--semantic-dim)' },
    interrupted:    { label: 'Interrupted',   color: 'var(--quality)', bg: 'var(--quality-dim)' },
    degraded_input: { label: 'Degraded',      color: 'var(--quality)', bg: 'var(--quality-dim)' },
    skipped:        { label: 'Skipped',       color: 'var(--ink-3)', bg: 'var(--fill-subtle)' },
    retried:        { label: 'Retried',       color: 'var(--ink-3)', bg: 'var(--fill-subtle)' },
  }
  return map[status] ?? { label: status, color: 'var(--ink-3)', bg: 'var(--fill-subtle)' }
}

function SectionHeader({ icon: Icon, label, color, count }: {
  icon: typeof AlertTriangle
  label: string
  color?: string
  count?: number
}) {
  return (
    <div className="flex items-center gap-1.5 mb-2">
      <Icon className="size-3" style={{ color: color ?? 'var(--muted-foreground)' }} />
      <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: color ?? 'var(--muted-foreground)' }}>
        {label}
      </span>
      {count !== undefined && count > 0 && (
        <span
          className="text-[10px] font-bold px-1.5 py-px rounded-full ml-0.5"
          style={{ background: 'var(--hover)', color: 'var(--muted-foreground)' }}
        >
          {count}
        </span>
      )}
    </div>
  )
}

function CollapsibleJson({ label, data }: { label: string; data: Record<string, unknown> | null }) {
  const [open, setOpen] = useState(false)
  if (!data || Object.keys(data).length === 0) return null
  return (
    <div>
      <button
        onClick={() => setOpen((p) => !p)}
        className="flex w-full items-center gap-1.5 py-1.5 text-left text-[13px] text-muted-foreground hover:text-foreground transition-colors"
      >
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        <span className="font-medium">{label}</span>
        <span className="font-mono text-[11px] text-muted-foreground/60">{Object.keys(data).length} keys</span>
      </button>
      {open && (
        <div className="mt-1.5 mb-1">
          <JsonViewer data={data} />
        </div>
      )}
    </div>
  )
}

/* ── Primary Finding ────────────────────────────────────────────── */

function derivePrimaryFinding(step: NodeEvent): { text: string; color: string } | null {
  if (step.status === 'crashed' && step.exception) {
    const first = step.exception.split('\n').filter(Boolean).pop() ?? step.exception
    return { text: first.trim(), color: 'var(--tool)' }
  }
  const insp = step.inspection
  if (!insp) return null

  const criticals = insp.tool_failures.filter((f) => f.severity === 'critical')
  if (criticals.length > 0) {
    const meta = getFailureMeta(criticals[0].failure_type)
    return {
      text: `${meta.label}: ${criticals[0].evidence}`,
      color: 'var(--tool)',
    }
  }
  if (insp.is_silent_failure && insp.missing_fields.length > 0) {
    return {
      text: `Missing fields will break downstream: ${insp.missing_fields.join(', ')}`,
      color: 'var(--tool)',
    }
  }
  if (step.status === 'semantic_fail') {
    const reason = step.semantic_check?.reason ?? 'Output failed semantic coherence check'
    return { text: reason, color: 'var(--semantic)' }
  }
  const warnings = insp.tool_failures.filter((f) => f.severity === 'warning')
  if (warnings.length > 0) {
    const meta = getFailureMeta(warnings[0].failure_type)
    return {
      text: `${meta.label}: ${warnings[0].evidence}`,
      color: 'var(--quality)',
    }
  }
  return null
}

/* ── Decision Explanation ───────────────────────────────────────── */

function NodeDetail({ step, run, onDismiss }: { step: NodeEvent; run: RunRecord; onDismiss?: () => void }) {
  const badge = statusBadge(step.status)
  const hasTokens = (step.llm_usage?.total_tokens ?? 0) > 0
  const hasCost = (step.llm_usage?.total_cost_usd ?? 0) > 0
  const hasSemantic = !!step.semantic_check
  const hasValidators = step.validator_results.length > 0
  const hasException = !!step.exception

  // Collect all issues
  const toolFailures = step.inspection?.tool_failures ?? []
  const semanticSignals = step.inspection?.semantic_signals ?? []
  const missingFields = step.inspection?.missing_fields ?? []
  const emptyFields = step.inspection?.empty_fields ?? []
  const typeMismatches = step.inspection?.type_mismatches ?? []
  const degradedFields = step.inspection?.degraded_fields ?? []
  const degradedUpstream = step.inspection?.degraded_upstream_node ?? null
  const anomalySignals = step.anomaly_signals ?? []

  const totalIssues = toolFailures.length + semanticSignals.length + missingFields.length + typeMismatches.length
  const hasIssues = totalIssues > 0
  const hasDegradedInfo = degradedFields.length > 0 || degradedUpstream !== null
  const hasAnomalies = anomalySignals.length > 0

  const primaryFinding = derivePrimaryFinding(step)

  // Build inline metrics
  const metrics: { label: string; value: string }[] = [
    { label: 'Duration', value: formatDur(step.duration_ms) },
  ]
  if (hasTokens) metrics.push({ label: 'Tokens', value: step.llm_usage!.total_tokens.toLocaleString() })
  if (hasCost) metrics.push({ label: 'Cost', value: `$${step.llm_usage!.total_cost_usd!.toFixed(4)}` })
  if (step.attempt_index > 0) metrics.push({ label: 'Attempt', value: `#${step.attempt_index + 1}` })

  return (
    <div className="rounded-[10px] border border-border bg-card overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
        <div className="flex items-center gap-2">
          <Info className="size-3.5 text-muted-foreground" />
          <span className="text-[13px] font-medium text-muted-foreground">Node Inspector</span>
        </div>
        {onDismiss && (
          <button
            onClick={onDismiss}
            className="rounded-md p-1 text-muted-foreground hover:text-foreground transition-colors"
          >
            <XIcon className="size-3.5" />
          </button>
        )}
      </div>

      {/* Content */}
      <div className="px-5 py-4">
        {/* Node name + status */}
        <div className="flex items-start justify-between gap-3 mb-1">
          <h3 className="font-mono text-[17px] font-bold text-foreground leading-tight">
            {step.node_name}
          </h3>
          <span
            className="shrink-0 mt-0.5"
            style={{
              fontSize: 11, fontWeight: 600, color: badge.color, background: badge.bg,
              padding: '2px 10px', borderRadius: 999,
              display: 'inline-flex', alignItems: 'center', gap: 5,
            }}
          >
            <span style={{ width: 5, height: 5, borderRadius: '50%', background: badge.color }} />
            {badge.label}
          </span>
        </div>

        {/* Subtitle: step + behavior + metrics */}
        <div className="flex items-center gap-0 text-[12.5px] text-muted-foreground flex-wrap">
          <span>Step {step.step_index + 1}</span>
          {step.behavior_type && (
            <>
              <span className="mx-1.5 text-border">·</span>
              <span className="font-mono text-[11.5px]" style={{ color: 'var(--iris-bright)' }}>{step.behavior_type}</span>
            </>
          )}
          {metrics.map((m) => (
            <span key={m.label} className="contents">
              <span className="mx-1.5 text-border">·</span>
              <span>
                <span className="text-muted-foreground">{m.label} </span>
                <span className="font-mono tabular-nums text-foreground">{m.value}</span>
              </span>
            </span>
          ))}
        </div>

        {/* Primary Finding — headline */}
        {primaryFinding && (
          <div
            className="mt-4 px-3 py-2.5 rounded-lg flex items-start gap-2"
            style={{
              background: `color-mix(in srgb, ${primaryFinding.color} 5%, transparent)`,
              border: `1px solid color-mix(in srgb, ${primaryFinding.color} 15%, transparent)`,
            }}
          >
            <span className="mt-0.5 shrink-0 size-2 rounded-full" style={{ background: primaryFinding.color }} />
            <p className="text-[13px] leading-relaxed" style={{ color: primaryFinding.color }}>
              {primaryFinding.text}
            </p>
          </div>
        )}

        {/* Decision Explanation — synthesized "why" */}
        <DecisionExplanation step={step} run={run} />

        {/* Sections */}
        <div className="mt-5 flex flex-col gap-5">

          {/* ── Detected Issues ─────────────────────────────────── */}
          {hasIssues && (
            <div>
              <SectionHeader icon={AlertTriangle} label="Detected Issues" count={totalIssues} />
              <div className="flex flex-col gap-1.5">
                {/* Missing fields */}
                {missingFields.length > 0 && (
                  <div
                    className="flex items-start gap-2.5 px-3 py-2 rounded-lg"
                    style={{
                      background: 'color-mix(in srgb, var(--tool) 4%, transparent)',
                      border: '1px solid color-mix(in srgb, var(--tool) 12%, transparent)',
                    }}
                  >
                    <span className="mt-1.5 shrink-0 size-1.5 rounded-full" style={{ background: 'var(--tool)' }} />
                    <div className="min-w-0">
                      <span className="text-[13px] font-medium text-foreground">Missing Required Fields</span>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {missingFields.map((f) => (
                          <code
                            key={f}
                            className="rounded px-1.5 py-0.5 font-mono text-[11px]"
                            style={{ background: 'var(--tool-dim)', color: 'var(--tool)' }}
                          >
                            {f}
                          </code>
                        ))}
                      </div>
                    </div>
                  </div>
                )}

                {/* Type mismatches */}
                {typeMismatches.map((m, i) => (
                  <div
                    key={i}
                    className="flex items-start gap-2.5 px-3 py-2 rounded-lg"
                    style={{
                      background: 'color-mix(in srgb, var(--quality) 4%, transparent)',
                      border: '1px solid color-mix(in srgb, var(--quality) 12%, transparent)',
                    }}
                  >
                    <span className="mt-1.5 shrink-0 size-1.5 rounded-full" style={{ background: 'var(--quality)' }} />
                    <div className="min-w-0">
                      <span className="text-[13px] font-medium text-foreground">Type Mismatch</span>
                      <div className="text-[12px] font-mono mt-0.5">
                        <span className="text-blue-400">{m.field_name}</span>
                        <span className="text-muted-foreground"> expected </span>
                        <span className="text-green-400">{m.expected_type}</span>
                        <span className="text-muted-foreground"> got </span>
                        <span className="text-amber-400">{m.actual_type}</span>
                      </div>
                    </div>
                  </div>
                ))}

                {/* Tool failures */}
                {toolFailures.map((tf, i) => (
                  <ToolFailureRow key={`tf-${i}`} tf={tf} />
                ))}

                {/* Semantic signals */}
                {semanticSignals.map((sig, i) => (
                  <SemanticSignalRow key={`sig-${i}`} sig={sig} />
                ))}
              </div>

              {/* Empty fields — subtle */}
              {emptyFields.length > 0 && (
                <div className="mt-2 text-[12px] text-muted-foreground/70">
                  <span>Empty optional: </span>
                  <span className="font-mono">{emptyFields.join(', ')}</span>
                </div>
              )}
            </div>
          )}

          {/* ── Degraded Input ──────────────────────────────────── */}
          {hasDegradedInfo && (
            <div>
              <SectionHeader icon={ArrowRight} label="Degraded Input" color="var(--quality)" />
              <div
                className="px-3 py-2.5 rounded-lg text-[13px]"
                style={{
                  background: 'color-mix(in srgb, var(--quality) 4%, transparent)',
                  border: '1px solid color-mix(in srgb, var(--quality) 12%, transparent)',
                }}
              >
                {degradedUpstream && (
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-muted-foreground">Upstream culprit:</span>
                    <code
                      className="font-mono text-[11px] font-bold px-1.5 py-0.5 rounded"
                      style={{ background: 'var(--quality-dim)', color: 'var(--quality)' }}
                    >
                      {degradedUpstream}
                    </code>
                  </div>
                )}
                {degradedFields.length > 0 && (
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-muted-foreground">Affected fields:</span>
                    {degradedFields.map((f) => (
                      <code
                        key={f}
                        className="font-mono text-[11px] px-1.5 py-0.5 rounded"
                        style={{ background: 'var(--quality-dim)', color: 'var(--quality)' }}
                      >
                        {f}
                      </code>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* ── Semantic Check ──────────────────────────────────── */}
          {hasSemantic && (
            <div>
              <SectionHeader icon={Brain} label="Semantic Check" />
              <div className="flex items-center gap-2 mb-1">
                {step.semantic_check!.passed ? (
                  <Check className="size-3.5 text-[var(--ok)]" />
                ) : (
                  <XIcon className="size-3.5 text-[var(--tool)]" />
                )}
                <span className="text-[13px] font-semibold text-foreground">
                  {step.semantic_check!.passed ? 'Coherent' : 'Incoherent'}
                </span>
                <span className="font-mono text-[11px] text-muted-foreground">
                  {Math.round(step.semantic_check!.confidence * 100)}%
                </span>
              </div>
              <p className="text-[12.5px] text-muted-foreground leading-relaxed">
                {step.semantic_check!.reason}
              </p>
              {(step.semantic_check!.evidence_considered?.length ?? 0) > 0 && (
                <div className="mt-2 space-y-1">
                  <p className="text-[11px] font-medium text-muted-foreground uppercase tracking-wide">Evidence Considered</p>
                  {step.semantic_check!.evidence_considered!.map((e, i) => (
                    <p key={i} className="text-[12px] text-muted-foreground pl-2 border-l-2 border-border">{e}</p>
                  ))}
                </div>
              )}
              {(step.semantic_check!.overridden_signals?.length ?? 0) > 0 && (
                <div className="mt-2 space-y-1">
                  <p className="text-[11px] font-medium text-[var(--quality)] uppercase tracking-wide">Overridden Signals</p>
                  {step.semantic_check!.overridden_signals!.map((s, i) => (
                    <p key={i} className="text-[12px] text-[var(--quality)] pl-2 border-l-2 border-[var(--quality)]/30">{s}</p>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ── Anomaly Signals ─────────────────────────────────── */}
          {hasAnomalies && (
            <div>
              <SectionHeader icon={Activity} label="Anomaly Signals" count={anomalySignals.length} />
              <div className="flex flex-col gap-1.5">
                {anomalySignals.map((a, i) => {
                  const sevColor = a.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'
                  return (
                    <div
                      key={i}
                      className="px-3 py-2.5 rounded-lg"
                      style={{
                        background: `color-mix(in srgb, ${sevColor} 4%, transparent)`,
                        border: `1px solid color-mix(in srgb, ${sevColor} 12%, transparent)`,
                      }}
                    >
                      <div className="flex items-center gap-2 mb-1">
                        <span className="size-1.5 rounded-full" style={{ background: sevColor }} />
                        <span className="text-[12px] font-semibold" style={{ color: sevColor }}>{a.severity}</span>
                        <span className="font-mono text-[11px] text-muted-foreground">
                          suspicion {(a.suspicion_score * 100).toFixed(0)}%
                        </span>
                      </div>
                      <p className="text-[13px] text-foreground/90 leading-relaxed">{a.reason}</p>
                      {(a.expected_behavior || a.observed_behavior) && (
                        <div className="mt-1.5 grid grid-cols-2 gap-3 text-[11.5px]">
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

          {/* ── Validators ──────────────────────────────────────── */}
          {hasValidators && (
            <div>
              <SectionHeader icon={ShieldCheck} label="Validators" />
              <div className="flex flex-col gap-1.5">
                {step.validator_results.map((v, i) => (
                  <div key={i} className="flex items-start gap-2">
                    {v.is_valid ? (
                      <Check className="size-3 mt-0.5 text-[var(--ok)] shrink-0" />
                    ) : (
                      <XIcon className="size-3 mt-0.5 text-[var(--tool)] shrink-0" />
                    )}
                    <div className="min-w-0">
                      <span className="font-mono text-[12px] font-medium text-foreground">{v.validator_name}</span>
                      {v.message && (
                        <span className="text-[11.5px] text-muted-foreground ml-1.5">{v.message}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Exception ───────────────────────────────────────── */}
          {hasException && (
            <div>
              <SectionHeader icon={Bug} label="Exception" color="var(--tool)" />
              <div
                className="rounded-lg overflow-hidden"
                style={{
                  background: 'color-mix(in srgb, var(--failure) 3%, transparent)',
                  border: '1px solid color-mix(in srgb, var(--failure) 25%, transparent)',
                }}
              >
                <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border/40">
                  <span className="size-2 rounded-full" style={{ background: 'var(--tool)' }} />
                  <span className="size-2 rounded-full" style={{ background: 'var(--quality)' }} />
                  <span className="size-2 rounded-full" style={{ background: 'var(--ok)' }} />
                  <span className="font-mono text-[10px] text-muted-foreground ml-0.5">traceback</span>
                </div>
                <div className="px-3 py-2.5 overflow-x-auto">
                  <pre className="font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all" style={{ color: 'var(--tool)' }}>
                    {step.exception}
                  </pre>
                </div>
              </div>
            </div>
          )}

          {/* ── Input / Output ──────────────────────────────────── */}
          {(step.input_state || step.output_dict) && (
            <div className="border-t border-border pt-4 flex flex-col gap-1">
              <CollapsibleJson label="Input State" data={step.input_state} />
              <CollapsibleJson label="Output" data={step.output_dict} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function StepInspector({
  run,
  selectedNodeName,
  onDismiss,
}: {
  run: RunRecord
  selectedNodeName?: string | null
  onDismiss?: () => void
}) {
  const steps = run.steps ?? []

  if (selectedNodeName) {
    const step = steps.find((s) => s.node_name === selectedNodeName)
    if (step) return <NodeDetail step={step} run={run} onDismiss={onDismiss} />
  }

  const failedStep = steps.find((s) => s.status !== 'pass')
  if (!failedStep) return null

  return <NodeDetail step={failedStep} run={run} />
}
