'use client'

import type { RunRecord } from '@/lib/types'
import { computeDiffs, computeSummaryMetrics, computeChangeImpact, computeKeyChanges, computeStructuralAnalysis, isReplayOf, runBLabel } from '../lib/compare-utils'
import SummaryMetrics from '../components/SummaryMetrics'
import PipelineComparison from '../components/PipelineComparison'
import KeyChangesSummary from '../components/KeyChangesSummary'
import ChangeImpactChart from '../components/ChangeImpactChart'
import NodeComparisonTable from '../components/NodeComparisonTable'
import StructuredDiff from '../components/StructuredDiff'

export default function OverviewTab({ runA, runB }: { runA: RunRecord; runB: RunRecord }) {
  const { nodes } = computeDiffs(runA, runB)
  const summaryMetrics = computeSummaryMetrics(runA, runB)
  const impact = computeChangeImpact(nodes)
  const keyChanges = computeKeyChanges(nodes)
  const structural = computeStructuralAnalysis(runA, runB)
  const replay = isReplayOf(runA, runB)
  const bLabel = runBLabel(runA, runB)

  const infoBannerText = structural.isStructurallyDifferent
    ? `These runs have different pipeline structures (${Math.round(structural.overlapRatio * 100)}% node overlap). Node-level comparison is limited to shared nodes.`
    : replay
      ? 'Comparison shows differences between the original run and its replay.'
      : 'Comparison shows differences between two executions of the same pipeline.'

  return (
    <div className="space-y-2.5 py-2.5">
      {/* Structural warning */}
      {structural.isStructurallyDifferent && (
        <div
          className="flex items-center gap-2 px-4 py-2.5 rounded-lg text-[12px] font-medium"
          style={{ background: 'color-mix(in srgb, var(--primary) 6%, transparent)', color: 'var(--iris)', border: '1px solid color-mix(in srgb, var(--primary) 15%, transparent)' }}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M7 2l5.5 9H1.5L7 2Z" stroke="currentColor" strokeWidth="1.2" fill="none" strokeLinejoin="round"/>
            <path d="M7 5.5v2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
            <circle cx="7" cy="10" r="0.5" fill="currentColor"/>
          </svg>
          Different pipeline structures detected ({Math.round(structural.overlapRatio * 100)}% node overlap).
          {structural.onlyInA.length > 0 && ` ${structural.onlyInA.length} node${structural.onlyInA.length !== 1 ? 's' : ''} only in Run A.`}
          {structural.onlyInB.length > 0 && ` ${structural.onlyInB.length} node${structural.onlyInB.length !== 1 ? 's' : ''} only in ${bLabel}.`}
        </div>
      )}

      {/* Summary Metrics */}
      <SummaryMetrics metrics={summaryMetrics} />

      {/* Main compare layout */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-2.5">
        <div className="lg:col-span-7 flex flex-col gap-2.5">
          <PipelineComparison runA={runA} runB={runB} diffs={nodes} />
          <NodeComparisonTable diffs={nodes} runBColumnLabel={bLabel} />
        </div>
        <div className="lg:col-span-5 flex flex-col gap-2.5">
          <div className="rounded-[10px] border border-border bg-card p-3.5">
            <div className="min-w-0">
              <h3 className="text-[13px] font-bold mb-2" style={{ color: 'var(--foreground)' }}>Key Changes Summary</h3>
              <KeyChangesSummary changes={keyChanges} compact />
            </div>
            <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--border)' }}>
              <h3 className="text-[13px] font-bold mb-2" style={{ color: 'var(--foreground)' }}>Change Impact</h3>
              <ChangeImpactChart impact={impact} compact />
            </div>
          </div>
          <StructuredDiff diffs={nodes} />
        </div>
      </div>

      {/* Info banner */}
      <div
        className="flex items-center gap-2 px-4 py-2.5 rounded-lg text-[12px]"
        style={{ background: 'var(--card)', color: 'var(--text-tertiary)' }}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <circle cx="7" cy="7" r="5.5" stroke="currentColor" strokeWidth="1.1"/>
          <path d="M7 4.5v3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
          <circle cx="7" cy="9.5" r="0.6" fill="currentColor"/>
        </svg>
        {infoBannerText}
      </div>
    </div>
  )
}
