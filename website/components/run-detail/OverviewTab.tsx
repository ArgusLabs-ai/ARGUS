'use client'

import { useState } from 'react'
import type { RunRecord, RunSummary } from '@/lib/types'
import { ChevronRight } from 'lucide-react'
import ExecutionGraph from './ExecutionGraph'
import RunMetricsBar from './RunMetricsBar'
import StepInspector from './StepInspector'
import ReplayBranches from './ReplayBranches'

function AIAnalysisSummaryCard({ run, onViewFull }: { run: RunRecord; onViewFull: () => void }) {
  const inv = run.llm_investigation
  if (!inv || !inv.triggered) {
    return (
      <div className="rounded-xl border border-border bg-card p-4">
        <h3 style={{ fontSize: 14, fontWeight: 700, letterSpacing: '-0.02em', marginBottom: 6 }} className="text-foreground">AI Analysis</h3>
        <p className="text-[12px] text-muted-foreground">No AI analysis available for this run.</p>
      </div>
    )
  }

  const confPct = Math.round(inv.confidence * 100)
  const confColor = inv.confidence >= 0.75
    ? { color: 'var(--ok)', bg: 'var(--ok-dim)', border: 'color-mix(in srgb, var(--ok) 34%, transparent)' }
    : inv.confidence >= 0.45
      ? { color: 'var(--quality)', bg: 'var(--quality-dim)', border: 'color-mix(in srgb, var(--quality) 34%, transparent)' }
      : { color: 'var(--ink-3)', bg: 'var(--fill-subtle)', border: 'var(--line-2)' }
  const rootCauseNode = run.first_failure_step ?? run.root_cause_chain?.[0]
  const rootCauseStep = run.steps?.findIndex((s) => s.node_name === rootCauseNode)

  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="mb-2.5 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <path d="M8 1l1.5 3.5L13 6l-3 2 .5 4L8 10.5 5.5 12l.5-4-3-2 3.5-1.5L8 1Z" fill="color-mix(in srgb, var(--primary) 10%, transparent)" stroke="var(--primary)" strokeWidth="1" />
          </svg>
          <span className="text-[14px] font-bold text-primary" style={{ letterSpacing: '-0.02em' }}>AI Analysis</span>
        </div>
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: confColor.color,
            background: confColor.bg,
            border: `1px solid ${confColor.border}`,
            padding: '2px 10px',
            borderRadius: 999,
          }}
        >
          {confPct}%
        </span>
      </div>

      <div className="flex flex-col gap-1">
        {rootCauseNode && (
          <p className="text-[13px] text-foreground" style={{ lineHeight: 1.4 }}>
            <span style={{ fontWeight: 600 }}>Root cause: </span>
            <span className="font-mono font-semibold" style={{ color: 'var(--tool)' }}>{rootCauseNode}</span>
            {rootCauseStep !== undefined && rootCauseStep >= 0 && (
              <span className="text-muted-foreground"> (step {rootCauseStep + 1})</span>
            )}
          </p>
        )}
        {inv.root_cause_explanation && (
          <p className="text-[12px] text-muted-foreground" style={{ lineHeight: 1.5, maxWidth: 500 }}>
            {inv.root_cause_explanation.length > 120
              ? inv.root_cause_explanation.slice(0, 120) + '...'
              : inv.root_cause_explanation}
          </p>
        )}
      </div>

      <button
        onClick={onViewFull}
        className="mt-2.5 flex items-center gap-1 border-none bg-transparent p-0 text-[12px] font-semibold text-primary cursor-pointer"
      >
        View Full Analysis
        <ChevronRight size={12} />
      </button>
    </div>
  )
}

type Tab = 'Overview' | 'Pipeline' | 'AI Analysis' | 'Correlations' | 'State' | 'Logs'

function UnannotatedBanner({ run }: { run: RunRecord }) {
  const steps = run.steps ?? []
  const unannotatedSteps = steps.filter(
    (s) => (s.inspection?.unannotated_successors?.length ?? 0) > 0
  )
  if (unannotatedSteps.length === 0) return null

  // Only show if most/all steps have this issue
  const ratio = unannotatedSteps.length / steps.length
  if (ratio < 0.5) return null

  return (
    <div
      className="rounded-xl border px-4 py-3 flex items-start gap-3"
      style={{
        background: 'var(--iris-dim)',
        borderColor: 'color-mix(in srgb, var(--iris) 34%, transparent)',
      }}
    >
      <span className="text-[18px] leading-none mt-0.5">💡</span>
      <div className="min-w-0">
        <p className="text-[13px] font-semibold text-foreground" style={{ lineHeight: 1.4 }}>
          Add type annotations to unlock full silent-failure detection
        </p>
        <p className="text-[12px] text-muted-foreground mt-1" style={{ lineHeight: 1.5 }}>
          {unannotatedSteps.length} of {steps.length} steps have unannotated successors — ARGUS can&apos;t check
          if the right fields are being passed between nodes. Add a <code className="font-mono text-[11px] px-1 py-0.5 rounded" style={{ background: 'var(--iris-dim)', color: 'var(--iris-bright)' }}>TypedDict</code> annotation
          to your node functions&apos; <code className="font-mono text-[11px] px-1 py-0.5 rounded" style={{ background: 'var(--iris-dim)', color: 'var(--iris-bright)' }}>state</code> parameter to enable this.
        </p>
      </div>
    </div>
  )
}

export default function OverviewTab({ run, allRuns, onSwitchTab }: { run: RunRecord; allRuns: RunSummary[]; onSwitchTab: (tab: Tab) => void }) {
  const [selectedNode, setSelectedNode] = useState<string | null>(null)

  return (
    <div className="flex flex-col gap-6 p-5">
      <UnannotatedBanner run={run} />

      <ExecutionGraph run={run} onViewFull={() => onSwitchTab('Pipeline')} onSelectNode={setSelectedNode} />

      <AIAnalysisSummaryCard run={run} onViewFull={() => onSwitchTab('AI Analysis')} />

      <RunMetricsBar run={run} />

      <StepInspector run={run} selectedNodeName={selectedNode} onDismiss={() => setSelectedNode(null)} />

      <ReplayBranches run={run} allRuns={allRuns} onSwitchTab={onSwitchTab} />
    </div>
  )
}
