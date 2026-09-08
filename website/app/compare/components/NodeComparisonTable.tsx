'use client'

import { formatDur } from '@/lib/run-utils'
import type { NodeDiff } from '../lib/compare-utils'
import { STEP_ICON } from '../lib/compare-utils'

function formatDelta(bMs: number | undefined, aMs: number | undefined): string {
  if (bMs === undefined || aMs === undefined) return '-'
  const delta = aMs - bMs
  const pct = bMs > 0 ? ((delta / bMs) * 100).toFixed(1) : '0'
  const sign = delta >= 0 ? '+' : ''
  if (Math.abs(delta) < 50) return '-'
  if (delta >= 1000 || delta <= -1000) return `${sign}${(delta / 1000).toFixed(2)}s (${sign}${pct}%)`
  return `${sign}${Math.round(delta)}ms (${sign}${pct}%)`
}

function impactLabel(diff: NodeDiff): { text: string; color: string } {
  if (diff.isFrozen) return { text: 'Only in A', color: 'var(--quality)' }
  if (diff.isNew) return { text: 'Only in B', color: 'var(--iris)' }
  if (diff.isFixed) return { text: 'Improved', color: 'var(--ok)' }
  if (diff.isRegression) return { text: 'Degraded', color: 'var(--tool)' }
  if (diff.inspectionDiffs.length > 0 || diff.fieldDiffs.length > 0) {
    const hasImprovement = diff.inspectionDiffs.some((d) => d.iconColor === 'var(--ok)')
    if (hasImprovement) return { text: 'Improved', color: 'var(--ok)' }
    return { text: 'Changed', color: 'var(--quality)' }
  }
  return { text: 'No change', color: 'var(--ink-3)' }
}

export default function NodeComparisonTable({ diffs, runBColumnLabel = 'Replay' }: { diffs: NodeDiff[]; runBColumnLabel?: string }) {
  return (
    <div className="card rounded-xl overflow-hidden">
      <div className="px-2.5 py-1.5" style={{ borderBottom: '1px solid var(--border)' }}>
        <h3 className="text-[11.5px] font-bold" style={{ color: 'var(--foreground)' }}>Node Comparison</h3>
      </div>

      <table className="w-full text-[10px]">
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            <th className="text-left px-2.5 py-1 font-semibold" style={{ color: 'var(--text-tertiary)', width: '30%' }}>Node</th>
            <th className="text-left px-1.5 py-1 font-semibold" style={{ color: 'var(--text-tertiary)', width: '15%' }}>Base</th>
            <th className="text-left px-1.5 py-1 font-semibold" style={{ color: 'var(--text-tertiary)', width: '15%' }}>{runBColumnLabel}</th>
            <th className="text-left px-1.5 py-1 font-semibold" style={{ color: 'var(--text-tertiary)', width: '22%' }}>Change</th>
            <th className="text-left px-1.5 py-1 font-semibold" style={{ color: 'var(--text-tertiary)', width: '18%' }}>Impact</th>
          </tr>
        </thead>
        <tbody>
          {diffs.map((diff, i) => {
            const beforeIcon = diff.before ? STEP_ICON[diff.before.status] : null
            const afterIcon = diff.after ? STEP_ICON[diff.after.status] : null
            const impact = impactLabel(diff)

            return (
              <tr
                key={diff.name}
                style={{ borderBottom: i < diffs.length - 1 ? '1px solid var(--border)' : 'none' }}
                className="transition-colors hover:bg-black/[0.02]"
              >
                <td className="px-2.5 py-1">
                  <div className="flex items-center gap-1">
                    <span className="text-[9px] font-bold tabular-nums" style={{ color: 'var(--text-tertiary)' }}>{i + 1}</span>
                    <span className="font-mono font-semibold truncate" style={{ color: 'var(--foreground)' }}>{diff.name}</span>
                    {beforeIcon && (
                      <span className="text-[9px]" style={{ color: beforeIcon.color }}>{beforeIcon.icon}</span>
                    )}
                  </div>
                </td>
                <td className="px-1.5 py-1">
                  <div className="flex items-center gap-1">
                    <span className="font-mono tabular-nums" style={{ color: 'var(--text-secondary)' }}>
                      {diff.before ? formatDur(diff.before.duration_ms) : '\u2014'}
                    </span>
                    {diff.before && (
                      <span className="text-[8.5px] font-semibold" style={{ color: (STEP_ICON[diff.before.status] ?? {}).color ?? 'var(--ink-3)' }}>
                        {diff.before.status === 'pass' ? 'OK' : diff.before.status === 'fail' ? 'Fail' : diff.before.status}
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-1.5 py-1">
                  <div className="flex items-center gap-1">
                    <span className="font-mono tabular-nums" style={{ color: 'var(--text-secondary)' }}>
                      {diff.after ? formatDur(diff.after.duration_ms) : '\u2014'}
                    </span>
                    {diff.after && (
                      <span className="text-[8.5px] font-semibold" style={{ color: (STEP_ICON[diff.after.status] ?? {}).color ?? 'var(--ink-3)' }}>
                        {diff.after.status === 'pass' ? 'OK' : diff.after.status === 'fail' ? 'Fail' : diff.after.status}
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-1.5 py-1 font-mono tabular-nums" style={{ color: 'var(--text-secondary)' }}>
                  {formatDelta(diff.before?.duration_ms, diff.after?.duration_ms)}
                </td>
                <td className="px-1.5 py-1">
                  <span className="text-[9px] font-bold" style={{ color: impact.color }}>
                    {impact.text}
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      <div className="px-2.5 py-1" style={{ borderTop: '1px solid var(--border)' }}>
        <span className="text-[10px] font-medium flex items-center gap-1 cursor-pointer" style={{ color: 'var(--iris)' }}>
          View all node details
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M3.5 2L6.5 5l-3 3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </span>
      </div>
    </div>
  )
}
