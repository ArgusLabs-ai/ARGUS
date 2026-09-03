'use client'

import { useState } from 'react'
import type { RunRecord } from '@/lib/types'

const colorForScore = (s: number) => s > 0.7 ? 'var(--failure)' : s > 0.3 ? 'var(--warning)' : 'var(--success)'

const BEHAVIOR_LABELS: Record<string, string> = {
  structured_json: 'Structured JSON',
  retrieval_result: 'Retrieval Result',
  classification: 'Classification',
  detailed_text: 'Detailed Text',
  tool_output: 'Tool Output',
  reasoning_chain: 'Reasoning Chain',
  chat_response: 'Chat Response',
  code_generation: 'Code Generation',
}

export default function BehaviorPanel({ run }: { run: RunRecord }) {
  const steps = run.steps ?? []
  const hasAnomalies = steps.some((s) => s.anomaly_signals && s.anomaly_signals.length > 0)
  const hasBehaviorTypes = steps.some((s) => s.behavior_type)
  const [expanded, setExpanded] = useState(false)

  if (!hasBehaviorTypes && !hasAnomalies) return null

  const totalAnomalies = steps.reduce((sum, s) => sum + (s.anomaly_signals?.length ?? 0), 0)
  const criticalAnomalies = steps.reduce(
    (sum, s) => sum + (s.anomaly_signals?.filter((a) => a.severity === 'critical').length ?? 0), 0,
  )
  const maxSuspicion = steps.reduce((max, s) => {
    const stepMax = Math.max(0, ...(s.anomaly_signals?.map((a) => a.suspicion_score) ?? []))
    return Math.max(max, stepMax)
  }, 0)

  const nodeRows = steps
    .filter((s) => s.behavior_type)
    .map((s) => ({
      name: s.node_name,
      behaviorType: s.behavior_type!,
      isOverride: run.behavior_config?.node_behaviors?.[s.node_name] != null,
      isPipelineDefault: run.behavior_config?.default_behavior_type === s.behavior_type,
      anomalyCount: s.anomaly_signals?.length ?? 0,
      maxScore: Math.max(0, ...(s.anomaly_signals?.map((a) => a.suspicion_score) ?? [])),
    }))

  return (
    <div>
      {/* Collapsible header */}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 py-2 hover:opacity-80 transition-opacity"
      >
        <span className="text-[11px] font-medium uppercase tracking-wider" style={{ color: 'var(--text-tertiary)' }}>
          Behavior
        </span>
        <div className="ml-auto flex items-center gap-3 text-[11px]">
          {totalAnomalies > 0 && (
            <span
              className="px-2 py-0.5 rounded-full font-medium"
              style={{
                color: criticalAnomalies > 0 ? 'var(--failure)' : 'var(--warning)',
                background: criticalAnomalies > 0 ? 'var(--tool-dim)' : 'var(--quality-dim)',
              }}
            >
              {totalAnomalies} anomal{totalAnomalies === 1 ? 'y' : 'ies'}
            </span>
          )}
          {maxSuspicion > 0 && (
            <span className="font-mono" style={{ color: colorForScore(maxSuspicion) }}>
              {(maxSuspicion * 100).toFixed(0)}% peak
            </span>
          )}
          <span className="text-[10px] text-muted-foreground/50">{expanded ? '▾' : '▸'}</span>
        </div>
      </button>

      {expanded && (
        <div className="rounded-xl border border-border bg-card overflow-hidden mt-1">
          <div className="p-4 space-y-4">
            {/* Pipeline config */}
            {run.behavior_config && (
              <div>
                <div className="text-[11px] font-medium uppercase tracking-wider mb-2" style={{ color: 'var(--text-tertiary)' }}>Configuration</div>
                <div className="flex items-baseline gap-4 flex-wrap text-[12px]">
                  <span className="text-muted-foreground">
                    pipeline default:{' '}
                    <span className="font-mono text-foreground">
                      {run.behavior_config.default_behavior_type
                        ? BEHAVIOR_LABELS[run.behavior_config.default_behavior_type] ?? run.behavior_config.default_behavior_type
                        : 'auto-infer'}
                    </span>
                  </span>
                  {Object.keys(run.behavior_config.node_behaviors ?? {}).length > 0 && (
                    <span className="text-muted-foreground">
                      overrides:{' '}
                      <span className="font-mono text-foreground">
                        {Object.entries(run.behavior_config.node_behaviors)
                          .map(([k, v]) => `${k}: ${v}`)
                          .join(', ')}
                      </span>
                    </span>
                  )}
                </div>
              </div>
            )}

            {/* Per-node table */}
            <div>
              <div className="text-[11px] font-medium uppercase tracking-wider mb-2" style={{ color: 'var(--text-tertiary)' }}>Per-Node Behavior</div>
              <div>
                {nodeRows.map((row, i) => (
                  <div
                    key={i}
                    className="flex items-baseline gap-0 py-2 text-[12px]"
                    style={{
                      borderBottom: i < nodeRows.length - 1 ? '1px solid var(--border)' : 'none',
                    }}
                  >
                    <span className="font-mono w-[140px] truncate shrink-0 text-muted-foreground">{row.name}</span>
                    <span
                      className={`w-[130px] shrink-0 ${row.isOverride ? 'font-semibold text-foreground' : 'text-muted-foreground'}`}
                    >
                      {BEHAVIOR_LABELS[row.behaviorType] ?? row.behaviorType}
                    </span>
                    <span className="text-[11px] px-1.5 py-0.5 rounded" style={{ color: 'var(--muted-foreground)', background: 'var(--card)' }}>
                      {row.isOverride ? 'override' : row.isPipelineDefault ? 'pipeline' : 'inferred'}
                    </span>
                    {row.anomalyCount > 0 && (
                      <span
                        className="ml-3 font-mono"
                        style={{ color: colorForScore(row.maxScore) }}
                      >
                        {row.anomalyCount} · {(row.maxScore * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
