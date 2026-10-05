'use client'

import type { RunRecord } from '@/lib/types'
import { formatDur, fmtTokens, fmtCost, displayNodes } from '@/lib/run-utils'

function formatStarted(iso: string): { time: string; ago: string } {
  try {
    const d = new Date(iso)
    const h = String(d.getHours()).padStart(2, '0')
    const m = String(d.getMinutes()).padStart(2, '0')
    const s = String(d.getSeconds()).padStart(2, '0')
    const now = new Date()
    const diffMs = now.getTime() - d.getTime()
    const diffMin = Math.round(diffMs / 60000)
    let ago: string
    if (diffMin < 1) ago = 'just now'
    else if (diffMin < 60) ago = `${diffMin}m ago`
    else if (diffMin < 1440) ago = `${Math.round(diffMin / 60)}h ago`
    else ago = `${Math.round(diffMin / 1440)}d ago`
    return { time: `${h}:${m}:${s}`, ago }
  } catch {
    return { time: iso, ago: '' }
  }
}

export default function RunMetricsBar({ run }: { run: RunRecord }) {
  const steps = run.steps ?? []
  const totalSteps = displayNodes(run.graph_node_names).length || steps.length
  const completedSteps = steps.length
  const failedCount = steps.filter((s) => s.status !== 'pass').length
  const totalTokens = run.total_tokens ?? 0
  const totalCost = run.total_cost_usd ?? null
  const started = formatStarted(run.started_at)

  const metrics = [
    {
      label: 'DURATION',
      value: formatDur(run.duration_ms),
      failed: false,
    },
    {
      label: 'STEPS',
      value: `${completedSteps}/${totalSteps}`,
      failed: failedCount > 0,
    },
    {
      label: 'TOKENS',
      value: totalTokens > 0 ? `${fmtTokens(totalTokens)}` : '\u2014',
      sub: totalTokens > 0 ? 'in+out' : undefined,
      failed: false,
    },
    {
      label: 'COST',
      value: totalCost != null ? fmtCost(totalCost) : '\u2014',
      failed: false,
    },
    {
      label: 'VERSION',
      value: `v${run.argus_version}`,
      sub: 'argus',
      failed: false,
    },
    {
      label: 'STARTED',
      value: started.time,
      sub: started.ago,
      failed: false,
    },
  ]

  return (
    <div className="flex flex-wrap items-center rounded-[10px] border border-border bg-card">
      {metrics.map((m, i) => (
        <div
          key={m.label}
          className={`flex flex-col gap-1 px-8 py-4${i !== 0 ? ' border-l border-border' : ''}`}
        >
          <div
            className="text-[11px] font-medium uppercase tracking-wider"
            style={{ color: 'var(--text-tertiary)' }}
          >
            {m.label}
          </div>
          <div className="flex items-baseline gap-1">
            <span
              className={`tnum text-xl font-bold${m.failed ? '' : ' text-foreground'}`}
              style={m.failed ? { color: 'var(--failure)' } : undefined}
            >
              {m.value}
            </span>
            {m.sub && (
              <span className="text-[13px] font-normal text-muted-foreground">
                {m.sub}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
