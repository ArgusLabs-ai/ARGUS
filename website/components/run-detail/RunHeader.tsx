'use client'

import { useState } from 'react'
import Link from 'next/link'
import type { RunRecord } from '@/lib/types'
import { STATUS_DOT, formatDur, formatTimestamp } from '@/lib/run-utils'
import SendReportDialog from '@/components/SendReportDialog'

const STATUS_COLOR: Record<string, string> = {
  clean: 'var(--ok)',
  silent_failure: 'var(--quality)',
  crashed: 'var(--tool)',
  semantic_fail: 'var(--semantic)',
  interrupted: 'var(--ink-3)',
}

export default function RunHeader({
  run,
  actions,
}: {
  run: RunRecord
  actions?: React.ReactNode
}) {
  const [showReport, setShowReport] = useState(false)
  const statusInfo = STATUS_DOT[run.overall_status] ?? { dot: '\u25CF', color: 'var(--ink-4)' }
  const steps = run.steps ?? []

  return (
    <div className="space-y-4">
      {/* Title row */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-xl font-bold tracking-tight flex items-center gap-3 text-foreground">
              <span style={{ color: statusInfo.color }} className="text-lg leading-none">{statusInfo.dot}</span>
              <span className="font-mono text-base text-foreground">{run.run_id}</span>
            </h1>
            {(() => {
              const sc = STATUS_COLOR[run.overall_status] ?? 'var(--ink-3)'
              return (
                <span
                  className="text-[11px] font-semibold px-2.5 py-1 rounded-full border"
                  style={{
                    background: `color-mix(in srgb, ${sc} 10%, transparent)`,
                    color: sc,
                    borderColor: `color-mix(in srgb, ${sc} 25%, transparent)`,
                  }}
                >
                  {run.overall_status.replace('_', ' ')}
                </span>
              )
            })()}
          </div>

          {/* Metadata row */}
          <div className="mt-3 flex items-center gap-2 flex-wrap text-[12px] text-muted-foreground">
            <span>{formatTimestamp(run.started_at)}</span>
            <span style={{ color: 'var(--text-tertiary)' }}>&middot;</span>
            <span>{formatDur(run.duration_ms)}</span>
            <span style={{ color: 'var(--text-tertiary)' }}>&middot;</span>
            <span>{steps.length} step{steps.length !== 1 ? 's' : ''}</span>
            <span style={{ color: 'var(--text-tertiary)' }}>&middot;</span>
            <span className="font-mono text-[11px]" style={{ color: 'var(--text-tertiary)' }}>v{run.argus_version}</span>
            {run.is_cyclic && (
              <>
                <span style={{ color: 'var(--text-tertiary)' }}>&middot;</span>
                <span style={{ color: 'var(--semantic)' }}>cyclic</span>
              </>
            )}
            {run.dry_run && (
              <>
                <span style={{ color: 'var(--text-tertiary)' }}>&middot;</span>
                <span style={{ color: 'var(--ink-3)' }}>dry run</span>
              </>
            )}
          </div>

          {/* Replay lineage */}
          {run.parent_run_id && (
            <div className="mt-2 text-[12px] text-muted-foreground">
              rerun of{' '}
              <a href={`/runs/${run.parent_run_id}`} className="font-mono hover:text-primary transition-colors">
                {run.parent_run_id}
              </a>
              {run.replay_from_step && (
                <>
                  {' '}from{' '}
                  <span className="font-mono font-semibold text-foreground">{run.replay_from_step}</span>
                </>
              )}
            </div>
          )}
        </div>

        {/* Action controls */}
        <div className="flex items-center gap-2 shrink-0">
          {actions}
          <button
            type="button"
            onClick={() => setShowReport(true)}
            className="inline-flex items-center gap-1.5 text-[12px] font-medium px-3.5 py-2 rounded-lg transition-colors"
            style={{ color: 'var(--text-secondary)', border: '1px solid var(--border)', background: 'var(--card)' }}
          >
            Report Issue
          </button>
          <Link
            href={`/compare?a=${run.run_id}`}
            className="inline-flex items-center gap-1.5 text-[12px] font-medium px-3.5 py-2 rounded-lg transition-colors"
            style={{ color: 'var(--text-secondary)', border: '1px solid var(--border)', background: 'var(--card)' }}
          >
            Compare
          </Link>
        </div>
      </div>

      <SendReportDialog open={showReport} onClose={() => setShowReport(false)} runId={run.run_id} />
    </div>
  )
}
