'use client'

import type { RunSummary } from '@/lib/types'
import { RunStatusBadge } from './StatusBadge'
import EvalBadge from './EvalBadge'
import type { EvalState } from './EvaluationBuilder'
import EmptyRunsState from './EmptyRunsState'
import { useServingInfo } from '@/lib/hooks'

function getRunShape(run: RunSummary): { label: string; color: string } | null {
  if (run.overall_status === 'clean' && !run.first_failure_step) {
    return { label: 'clean', color: 'var(--ok)' }
  }
  if (!run.first_failure_step) return null
  const firstNode = run.graph_node_names.find((n) => !n.startsWith('__'))
  if (run.first_failure_step === firstNode) {
    return { label: 'early fail', color: 'var(--tool)' }
  }
  return { label: 'partial', color: 'var(--quality)' }
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return '\u2014'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function truncateNodes(names: string[]): string {
  const filtered = names.filter((n) => !n.startsWith('__'))
  if (filtered.length <= 4) return filtered.join(' \u2192 ')
  return filtered.slice(0, 3).join(' \u2192 ') + ` +${filtered.length - 3}`
}

interface RunTableProps {
  runs: RunSummary[]
  evalState?: EvalState | null
}

const COL_HEADER_STYLE: React.CSSProperties = {
  fontSize: '11px',
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  color: 'var(--text-muted)',
  fontWeight: 600,
  paddingBottom: '12px',
  paddingTop: '12px',
}

export default function RunTable({ runs, evalState }: RunTableProps) {
  const serving = useServingInfo()
  if (runs.length === 0) {
    return <EmptyRunsState serving={serving} />
  }

  return (
    <div
      className="card overflow-x-auto rounded-xl"
    >
      <table className="w-full border-collapse" style={{ fontSize: '13px' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'left', paddingLeft: '20px', paddingRight: '24px' }}>Run ID</th>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'left', paddingRight: '24px' }}>Status</th>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'left', paddingRight: '24px' }} className="hidden lg:table-cell">Graph</th>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'right', paddingRight: '24px' }}>Steps</th>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'left', paddingRight: '24px' }}>Started</th>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'right', paddingRight: '24px' }}>Duration</th>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'left', paddingRight: '24px' }}>First Failure</th>
            <th style={{ ...COL_HEADER_STYLE, textAlign: 'right', paddingRight: '20px' }}>Shape</th>
            {evalState && (
              <th style={{ ...COL_HEADER_STYLE, textAlign: 'right', paddingRight: '20px' }}>Eval</th>
            )}
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const isFailed = run.overall_status !== 'clean'
            const shape = getRunShape(run)
            return (
              <tr
                key={run.run_id}
                className="group transition-colors"
                style={{
                  borderBottom: '1px solid var(--border-subtle)',
                  cursor: 'pointer',
                }}
                onMouseEnter={(e) => {
                  const el = e.currentTarget as HTMLTableRowElement
                  el.style.background = isFailed ? 'var(--tool-dim)' : 'var(--iris-dim)'
                }}
                onMouseLeave={(e) => {
                  const el = e.currentTarget as HTMLTableRowElement
                  el.style.background = 'transparent'
                }}
              >
                {/* Run ID */}
                <td className="py-3.5 pr-6 pl-5">
                  <a href={`/runs/${run.run_id}`} className="block">
                    <span className="font-mono text-[12px]" style={{ color: 'var(--text-primary)' }}>
                      {run.run_id.slice(0, 14)}
                      {run.run_id.length > 14 && (
                        <span style={{ color: 'var(--text-muted)' }}>&hellip;</span>
                      )}
                    </span>
                  </a>
                </td>

                {/* Status */}
                <td className="py-3.5 pr-6">
                  <a href={`/runs/${run.run_id}`} className="block">
                    <RunStatusBadge status={run.overall_status} />
                  </a>
                </td>

                {/* Graph */}
                <td className="py-3.5 pr-6 hidden lg:table-cell max-w-xs">
                  <a href={`/runs/${run.run_id}`} className="block">
                    <span
                      className="text-[12px] truncate block font-mono"
                      style={{ color: 'var(--text-muted)', maxWidth: '200px' }}
                    >
                      {truncateNodes(run.graph_node_names)}
                    </span>
                  </a>
                </td>

                {/* Steps */}
                <td className="py-3.5 pr-6 text-right">
                  <a href={`/runs/${run.run_id}`} className="block">
                    <span className="text-[12px] font-mono tabular-nums" style={{ color: 'var(--text-secondary)' }}>
                      {run.step_count}
                    </span>
                  </a>
                </td>

                {/* Started */}
                <td className="py-3.5 pr-6">
                  <a href={`/runs/${run.run_id}`} className="block">
                    <span className="text-[12px]" style={{ color: 'var(--text-secondary)' }} title={run.started_at}>
                      {relativeTime(run.started_at)}
                    </span>
                  </a>
                </td>

                {/* Duration */}
                <td className="py-3.5 pr-6 text-right">
                  <a href={`/runs/${run.run_id}`} className="block">
                    <span className="text-[12px] font-mono tabular-nums" style={{ color: 'var(--text-secondary)' }}>
                      {formatDuration(run.duration_ms)}
                    </span>
                  </a>
                </td>

                {/* First failure */}
                <td className="py-3.5 pr-6">
                  <a href={`/runs/${run.run_id}`} className="block">
                    {run.first_failure_step ? (
                      <span
                        className="text-[12px] font-mono font-medium"
                        style={{ color: 'var(--tool)' }}
                      >
                        {run.first_failure_step}
                      </span>
                    ) : (
                      <span style={{ color: 'var(--text-faint)' }}>&mdash;</span>
                    )}
                  </a>
                </td>

                {/* Shape */}
                <td className="py-3.5 text-right pr-5">
                  <a href={`/runs/${run.run_id}`} className="block">
                    {shape ? (
                      <span
                        className="text-[11px] font-medium px-2 py-0.5 rounded-md"
                        style={{ color: shape.color, background: `${shape.color}10` }}
                      >
                        {shape.label}
                      </span>
                    ) : (
                      <span style={{ color: 'var(--text-faint)' }}>&mdash;</span>
                    )}
                  </a>
                </td>

                {/* Eval */}
                {evalState && (
                  <td className="py-3.5 text-right pr-5">
                    <EvalBadge run={run} evalState={evalState} />
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
