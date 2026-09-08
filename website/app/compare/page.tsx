'use client'

import { useSearchParams, useRouter } from 'next/navigation'
import { useEffect, useState, Suspense } from 'react'
import type { RunSummary, RunRecord } from '@/lib/types'
import CompareHeader from './CompareHeader'
import CompareTabNav, { type TabId } from './CompareTabNav'
import OverviewTab from './tabs/OverviewTab'
import NodeComparisonTab from './tabs/NodeComparisonTab'
import DiffViewTab from './tabs/DiffViewTab'
import MetricsTab from './tabs/MetricsTab'
import AIAnalysisTab from './tabs/AIAnalysisTab'
import TimelineTab from './tabs/TimelineTab'
import LogsTab from './tabs/LogsTab'

function CompareContent() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const idA = searchParams.get('a') ?? ''
  const idB = searchParams.get('b') ?? ''

  const [allRuns, setAllRuns] = useState<RunSummary[]>([])
  const [runA, setRunA] = useState<RunRecord | null>(null)
  const [runB, setRunB] = useState<RunRecord | null>(null)
  const [activeTab, setActiveTab] = useState<TabId>('overview')

  // Load run list for selector
  useEffect(() => {
      fetch('/api/runs')
        .then((r) => r.json())
        .then((data: RunSummary[]) => setAllRuns(data))
        .catch(() => {})
  }, [])

  // Load the two selected runs
  useEffect(() => {
    if (!idA || !idB) {
      setRunA(null)
      setRunB(null)
      return
    }

      fetch(`/api/compare?a=${idA}&b=${idB}`)
        .then((r) => r.json())
        .then((data: { a: RunRecord | null; b: RunRecord | null }) => {
          setRunA(data.a)
          setRunB(data.b)
        })
        .catch(() => {})
  }, [idA, idB])

  function handleSelectA(id: string) {
    const params = new URLSearchParams(searchParams.toString())
    params.set('a', id)
    router.push(`/compare?${params.toString()}`)
  }

  function handleSelectB(id: string) {
    const params = new URLSearchParams(searchParams.toString())
    params.set('b', id)
    router.push(`/compare?${params.toString()}`)
  }

  const renderTab = () => {
    if (!runA || !runB) return null
    switch (activeTab) {
      case 'overview': return <OverviewTab runA={runA} runB={runB} />
      case 'node-comparison': return <NodeComparisonTab runA={runA} runB={runB} />
      case 'diff-view': return <DiffViewTab runA={runA} runB={runB} />
      case 'metrics': return <MetricsTab runA={runA} runB={runB} />
      case 'ai-analysis': return <AIAnalysisTab runA={runA} runB={runB} />
      case 'timeline': return <TimelineTab runA={runA} runB={runB} />
      case 'logs': return <LogsTab />
      default: return null
    }
  }

  return (
    <div className="space-y-0">
      {/* Page title + back button */}
      <div className="flex items-center justify-between mb-3">
        <div>
          <h1 className="text-[20px] font-bold tracking-[-0.02em]" style={{ color: 'var(--foreground)' }}>
            Compare Runs
          </h1>
          <p className="text-[13px] mt-0.5" style={{ color: 'var(--text-tertiary)' }}>
            Side-by-side comparison of pipeline executions
          </p>
        </div>
        <a
          href="/"
          className="text-[12px] font-medium flex items-center gap-1.5 px-3 py-1.5 rounded-lg hover:opacity-80 transition-opacity"
          style={{ color: 'var(--text-secondary)', border: '1px solid var(--border)' }}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M7.5 9L4.5 6l3-3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
          Back to run
        </a>
      </div>

      {/* Header: selectors + run info cards */}
      <CompareHeader
        runs={allRuns}
        selectedA={idA}
        selectedB={idB}
        runA={runA}
        runB={runB}
        onSelectA={handleSelectA}
        onSelectB={handleSelectB}
      />

      {/* Tab bar + active tab content */}
      {runA && runB && (
        <div className="mt-2">
          <CompareTabNav active={activeTab} onChange={setActiveTab} />
          {renderTab()}
        </div>
      )}

      {/* Empty states */}
      {(!runA || !runB) && idA && idB && (
        <div className="text-center py-16 text-sm font-mono" style={{ color: 'var(--text-tertiary)' }}>
          Could not load one or both runs.
        </div>
      )}

      {(!idA || !idB) && (
        <div className="text-center py-24 font-mono text-[12px]" style={{ color: 'var(--text-tertiary)' }}>
          Select two runs to compare
        </div>
      )}
    </div>
  )
}

export default function ComparePage() {
  return (
    <div className="px-5 py-4 overflow-auto h-full">
      <Suspense fallback={<div />}>
        <CompareContent />
      </Suspense>
    </div>
  )
}
