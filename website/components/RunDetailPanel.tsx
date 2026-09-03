'use client'

import { useState, useEffect } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowLeft, ScrollText, RefreshCw, GitBranch, AlertTriangle } from 'lucide-react'
import SendReportDialog from './SendReportDialog'
import type { RunRecord, RunSummary } from '@/lib/types'
import { useRunDetail } from '@/lib/hooks'
import { STATUS_DOT, formatDur, formatTimestamp } from '@/lib/run-utils'
import { RunStatusBadge } from '@/components/StatusBadge'
import ReplayControls from './run-detail/ReplayControls'
import RootCauseBanner from './run-detail/RootCauseBanner'
import MetricsGrid from './run-detail/MetricsGrid'
import RunMetricsBar from './run-detail/RunMetricsBar'
import ExecutionTimeline from './run-detail/ExecutionTimeline'
import AIAnalysisPanel from './run-detail/AIAnalysisPanel'
import CorrelationPanel from './run-detail/CorrelationPanel'
import BehaviorPanel from './run-detail/BehaviorPanel'
import OverviewTab from './run-detail/OverviewTab'
import JsonViewer from './JsonViewer'
import CliLogViewer from './CliLogViewer'

const TABS = ['Overview', 'Pipeline', 'AI Analysis', 'Correlations', 'State', 'Logs'] as const
type Tab = typeof TABS[number]

/* ── Logs Tab ────────────────────────────────────────────────── */

function LogsTab({ runId }: { runId: string }) {
  const [log, setLog] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`/api/logs/${runId}`)
      .then((r) => r.ok ? r.text() : null)
      .then((text) => { setLog(text); setLoading(false) })
      .catch(() => setLoading(false))
  }, [runId])

  if (loading) return <div className="p-8 text-center text-sm text-muted-foreground">Loading logs...</div>
  if (!log) return <div className="p-8 text-center text-sm text-muted-foreground">No logs available for this run.</div>

  return (
    <div className="p-5">
      <CliLogViewer log={log} runId={runId} />
    </div>
  )
}

/* ── Main Panel ──────────────────────────────────────────────── */

export default function RunDetailPanel({
  runId,
  previousRunId,
  onClose,
  allRuns,
  isOverlay = false,
}: {
  runId: string | null
  previousRunId?: string | null
  onClose: () => void
  allRuns: RunSummary[]
  isOverlay?: boolean
}) {
  const router = useRouter()
  const { run, loading, error } = useRunDetail(runId)
  const [activeTab, setActiveTab] = useState<Tab>('Overview')
  const [copied, setCopied] = useState(false)
  const [showReport, setShowReport] = useState(false)

  // Reset tab when run changes
  useEffect(() => { setActiveTab('Overview') }, [runId])

  if (!runId) {
    return (
      <div className="h-full flex flex-col items-center justify-center bg-background">
        <svg width="48" height="48" viewBox="0 0 18 18" fill="none" className="mb-4 opacity-20">
          <path d="M9 1.5L16.5 5.5V12.5L9 16.5L1.5 12.5V5.5L9 1.5Z" stroke="var(--iris)" strokeWidth="1.2" fill="none"/>
          <circle cx="9" cy="9" r="2.2" fill="var(--iris-dim)" stroke="var(--iris)" strokeWidth="1.1"/>
          <circle cx="9" cy="9" r="0.9" fill="var(--iris)"/>
        </svg>
        <p className="text-[14px] font-medium text-muted-foreground">Select a run to view details</p>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center bg-background">
        <div className="flex items-center gap-3">
          <div className="w-5 h-5 border-2 rounded-full animate-spin" style={{ borderColor: 'var(--fill-subtle)', borderTopColor: 'var(--iris)' }} />
          <span className="text-[13px] text-muted-foreground">Loading run...</span>
        </div>
      </div>
    )
  }

  if (error || !run) {
    return (
      <div className="h-full flex items-center justify-center bg-background">
        <p className="text-[13px] text-destructive">Error: {error ?? 'Run not found'}</p>
      </div>
    )
  }

  const statusInfo = STATUS_DOT[run.overall_status] ?? { dot: '\u25CF', color: 'var(--ink-4)' }
  const steps = run.steps ?? []
  const canReturnToPreviousRun = Boolean(previousRunId && previousRunId !== run.run_id)

  const copyRunId = () => {
    navigator.clipboard.writeText(run.run_id).catch(() => {})
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="h-full flex flex-col panel-slide-in bg-background">
      {/* Run header — reference style */}
      <div className="shrink-0 px-5 pt-3 pb-3 border-b border-border bg-background">
        {/* Back link */}
        <button
          onClick={onClose}
          className="inline-flex items-center gap-1 text-[12px] font-medium mb-2 transition-colors text-muted-foreground bg-transparent border-none cursor-pointer p-0"
        >
          <ArrowLeft className="size-3.5" />
          All runs
        </button>

        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-3 min-w-0">
              <span className="text-[20px] font-bold tracking-[-0.03em] leading-none truncate text-foreground">{run.run_id}</span>
              <RunStatusBadge status={run.overall_status} />
            </div>

            <div className="mt-1.5 flex items-center gap-1.5 flex-wrap text-[12px] font-medium text-muted-foreground">
              <span className="font-mono text-[11px]">{run.run_id.slice(0, 12)}</span>
              <span className="text-muted-foreground/50">&middot;</span>
              <span>Argus v{run.argus_version}</span>
              <span className="text-muted-foreground/50">&middot;</span>
              <span>{steps.length} steps</span>
              <span className="text-muted-foreground/50">&middot;</span>
              <span>{formatDur(run.duration_ms)}</span>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => setShowReport(true)}
              className="inline-flex items-center gap-1.5 text-[12px] font-semibold px-3 py-1.5 rounded-lg transition-colors border border-border text-muted-foreground bg-transparent hover:border-amber-500/40 hover:text-amber-400"
            >
              <AlertTriangle className="size-3.5" />
              Report Issue
            </button>
            <button
              className="inline-flex items-center gap-1.5 text-[12px] font-semibold px-3 py-1.5 rounded-lg transition-colors border border-border text-muted-foreground bg-transparent"
            >
              <ScrollText className="size-3.5" />
              Logs
            </button>
            <button
              className="inline-flex items-center gap-1.5 text-[12px] font-semibold px-3 py-1.5 rounded-lg transition-colors bg-primary text-white"
            >
              <RefreshCw className="size-3.5" />
              Replay
            </button>
          </div>
        </div>

        {run.parent_run_id && (
          <div className="mt-1.5 text-[12px] text-muted-foreground">
            rerun of{' '}
            <button
              onClick={() => {
                const parentRun = encodeURIComponent(run.parent_run_id ?? '')
                const currentRun = encodeURIComponent(run.run_id)
                router.replace(`/?run=${parentRun}&from=${currentRun}`, { scroll: false })
              }}
              className="font-mono transition-colors text-primary"
            >
              {run.parent_run_id}
            </button>
            {run.replay_from_step && (
              <> from <span className="font-mono font-semibold text-foreground">{run.replay_from_step}</span></>
            )}
          </div>
        )}
      </div>

      {/* Tabs — spec .tabs primitive */}
      <div className="tabs shrink-0 overflow-x-auto px-5" role="tablist" aria-label="Run detail sections">
        {TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={activeTab === tab}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto overscroll-contain bg-background">
        {activeTab === 'Overview' && (
          <OverviewTab run={run} allRuns={allRuns} onSwitchTab={setActiveTab} />
        )}

        {activeTab === 'Pipeline' && (
          <ReplayControls runId={run.run_id} run={run}>
            {(handleReplay, handleReplayNode, replayNodeState) => (
              <div className="p-5 space-y-6">
                {run.root_cause_chain && run.root_cause_chain.length > 0 && (
                  <RootCauseBanner chain={run.root_cause_chain} />
                )}
                <RunMetricsBar run={run} />
                <ExecutionTimeline
                  run={run}
                  onReplay={handleReplay}
                  onReplayNode={handleReplayNode}
                  replayingNode={replayNodeState.replayingNode}
                  nodeDiff={replayNodeState.nodeDiff}
                  onDismissDiff={replayNodeState.dismissDiff}
                />
              </div>
            )}
          </ReplayControls>
        )}

        {activeTab === 'AI Analysis' && (
          <div className="p-5">
            <AIAnalysisPanel run={run} />
          </div>
        )}

        {activeTab === 'Correlations' && (
          <div className="p-5">
            <CorrelationPanel run={run} />
          </div>
        )}

        {activeTab === 'State' && (
          <div className="p-5 space-y-6">
            {run.initial_state && Object.keys(run.initial_state).length > 0 && (
              <div>
                <div className="text-[11px] uppercase tracking-widest font-semibold mb-2 text-muted-foreground">Initial State</div>
                <JsonViewer data={run.initial_state} defaultCollapsed={true} />
              </div>
            )}
            <BehaviorPanel run={run} />
          </div>
        )}

        {activeTab === 'Logs' && (
          <LogsTab runId={run.run_id} />
        )}
      </div>

      <SendReportDialog open={showReport} onClose={() => setShowReport(false)} runId={run.run_id} />
    </div>
  )
}
