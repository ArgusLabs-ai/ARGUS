'use client'

import type { RunRecord } from '@/lib/types'
import { formatDur, fmtCost } from '@/lib/run-utils'
import { computeEvalMetrics } from '../lib/compare-utils'

function MetricRow({ label, aVal, bVal, winner }: { label: string; aVal: string; bVal: string; winner: 'A' | 'B' | 'tie' }) {
  return (
    <div
      className="grid items-center py-3 px-4"
      style={{ gridTemplateColumns: '160px 1fr 1fr 60px', borderBottom: '1px solid var(--border)' }}
    >
      <span className="text-[13px] font-semibold" style={{ color: 'var(--foreground)' }}>{label}</span>
      <span className="text-[13px] font-mono tabular-nums" style={{ color: winner === 'A' ? 'var(--ok)' : 'var(--text-secondary)' }}>
        {aVal}
      </span>
      <span className="text-[13px] font-mono tabular-nums" style={{ color: winner === 'B' ? 'var(--ok)' : 'var(--text-secondary)' }}>
        {bVal}
      </span>
      <span className="text-[11px] text-right font-semibold" style={{ color: 'var(--ok)' }}>
        {winner !== 'tie' ? `${winner} \u2713` : ''}
      </span>
    </div>
  )
}

export default function MetricsTab({ runA, runB }: { runA: RunRecord; runB: RunRecord }) {
  const { a, b } = computeEvalMetrics(runA, runB)

  function cellWinner(aVal: number, bVal: number, higherIsBetter = false): 'A' | 'B' | 'tie' {
    if (aVal === bVal) return 'tie'
    return (higherIsBetter ? aVal > bVal : aVal < bVal) ? 'A' : 'B'
  }

  const rows: { label: string; aVal: string; bVal: string; winner: 'A' | 'B' | 'tie' }[] = [
    { label: 'Failures', aVal: String(a.failureCount), bVal: String(b.failureCount), winner: cellWinner(a.failureCount, b.failureCount) },
    { label: 'Success Rate', aVal: `${a.successRate}%`, bVal: `${b.successRate}%`, winner: cellWinner(a.successRate, b.successRate, true) },
    { label: 'Duration', aVal: formatDur(runA.duration_ms), bVal: formatDur(runB.duration_ms), winner: cellWinner(runA.duration_ms ?? 0, runB.duration_ms ?? 0) },
    { label: 'Steps', aVal: String((runA.steps ?? []).length), bVal: String((runB.steps ?? []).length), winner: 'tie' },
  ]

  if (runA.total_cost_usd != null || runB.total_cost_usd != null) {
    const aCost = runA.total_cost_usd ?? 0
    const bCost = runB.total_cost_usd ?? 0
    rows.push({
      label: 'Cost',
      aVal: aCost > 0 ? fmtCost(aCost) : '\u2014',
      bVal: bCost > 0 ? fmtCost(bCost) : '\u2014',
      winner: cellWinner(aCost, bCost),
    })
  }

  if (runA.total_tokens != null || runB.total_tokens != null) {
    rows.push({
      label: 'Tokens',
      aVal: String(runA.total_tokens ?? 0),
      bVal: String(runB.total_tokens ?? 0),
      winner: cellWinner(runA.total_tokens ?? 0, runB.total_tokens ?? 0),
    })
  }

  return (
    <div className="py-4">
      <div className="rounded-[10px] border border-border bg-card overflow-hidden">
        {/* Header */}
        <div
          className="grid items-center py-2.5 px-4"
          style={{ gridTemplateColumns: '160px 1fr 1fr 60px', borderBottom: '2px solid var(--border)', background: 'var(--card)' }}
        >
          <span className="text-[11px] font-semibold uppercase tracking-widest" style={{ color: 'var(--text-tertiary)' }}>Metric</span>
          <span className="text-[11px] font-semibold uppercase tracking-widest" style={{ color: 'var(--text-tertiary)' }}>Base Run (A)</span>
          <span className="text-[11px] font-semibold uppercase tracking-widest" style={{ color: 'var(--text-tertiary)' }}>Replay (B)</span>
          <span className="text-[11px] font-semibold uppercase tracking-widest text-right" style={{ color: 'var(--text-tertiary)' }}>Winner</span>
        </div>
        {rows.map((row) => (
          <MetricRow key={row.label} {...row} />
        ))}
      </div>
    </div>
  )
}
