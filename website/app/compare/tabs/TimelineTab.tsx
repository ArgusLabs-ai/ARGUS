'use client'

import type { RunRecord } from '@/lib/types'
import { formatDur } from '@/lib/run-utils'
import { STEP_ICON } from '../lib/compare-utils'

function TimelineColumn({ run, label }: { run: RunRecord; label: string }) {
  const steps = run.steps ?? []
  return (
    <div className="rounded-[10px] border border-border bg-card overflow-hidden flex-1">
      <div className="px-4 py-2.5" style={{ borderBottom: '1px solid var(--border)', background: 'var(--card)' }}>
        <span className="text-[12px] font-bold" style={{ color: 'var(--foreground)' }}>{label}</span>
        <span className="text-[11px] font-mono ml-2" style={{ color: 'var(--text-tertiary)' }}>{steps.length} steps</span>
      </div>
      <div className="py-1">
        {steps.map((step, i) => {
          const icon = STEP_ICON[step.status]
          return (
            <div
              key={i}
              className="flex items-center gap-2 px-4 py-1.5 font-mono text-[12px]"
              style={{ borderBottom: i < steps.length - 1 ? '1px solid var(--border)' : 'none' }}
            >
              <span className="w-5 text-right tabular-nums" style={{ color: 'var(--text-tertiary)' }}>{i + 1}</span>
              <span style={{ color: icon?.color ?? 'var(--ink-3)' }}>{icon?.icon ?? '\u25CF'}</span>
              <span className="font-semibold" style={{ color: 'var(--foreground)' }}>{step.node_name}</span>
              <span className="ml-auto tabular-nums" style={{ color: 'var(--text-tertiary)' }}>{formatDur(step.duration_ms)}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function TimelineTab({ runA, runB }: { runA: RunRecord; runB: RunRecord }) {
  return (
    <div className="py-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
      <TimelineColumn run={runA} label="Base Run" />
      <TimelineColumn run={runB} label="Replay" />
    </div>
  )
}
