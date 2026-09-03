'use client'

import type { RunRecord } from '@/lib/types'
import type { NodeDiff } from '../lib/compare-utils'
import { STATUS_LABEL, isReplayOf } from '../lib/compare-utils'
import ExecutionGraph from '@/components/run-detail/ExecutionGraph'

export default function PipelineComparison({
  runA,
  runB,
  diffs,
}: {
  runA: RunRecord
  runB: RunRecord
  diffs: NodeDiff[]
}) {
  const isReplay = isReplayOf(runA, runB)
  const labelA = 'Base Run'
  const labelB = isReplay ? `Replay ${runB.run_id.includes('-R') ? runB.run_id.split('-R').pop() : '1'}` : 'Run B'

  const statusColorA = runA.overall_status === 'clean' ? 'var(--ok)' : 'var(--tool)'
  const statusColorB = runB.overall_status === 'clean' ? 'var(--ok)' : 'var(--tool)'

  return (
    <div className="flex flex-col gap-3">
      {/* Run A graph */}
      <div>
        <div className="flex items-center gap-2 mb-1.5 px-1">
          <span className="w-1.5 h-4 rounded-full shrink-0" style={{ background: statusColorA }} />
          <span className="text-[12px] font-semibold text-foreground">{labelA}</span>
          <span className="text-[11px] font-mono text-muted-foreground">{runA.run_id.slice(0, 12)}</span>
        </div>
        <ExecutionGraph run={runA} />
      </div>

      {/* Arrow separator */}
      <div className="flex justify-center py-0.5">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M8 3v10M5 10l3 3 3-3" stroke="var(--text-tertiary)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>

      {/* Run B graph */}
      <div>
        <div className="flex items-center gap-2 mb-1.5 px-1">
          <span className="w-1.5 h-4 rounded-full shrink-0" style={{ background: statusColorB }} />
          <span className="text-[12px] font-semibold text-foreground">{labelB}</span>
          <span className="text-[11px] font-mono text-muted-foreground">{runB.run_id.slice(0, 12)}</span>
        </div>
        <ExecutionGraph run={runB} />
      </div>
    </div>
  )
}
