'use client'

/* A run open in the workspace: mono title, status word, actions, one line
   of context, tabs. The body below is one of the tab views — all of them
   flush, none of them boxed. */

import { useState, useEffect } from 'react'
import type { RunSummary } from '@/lib/types'
import { useRunDetail } from '@/lib/hooks'
import { useWorkspace, toneFor, statusWord, formatDuration, shortRunId } from '@/lib/workspace'
import { fmtClock } from '@/lib/run-detail'
import { pipelineLabel } from '@/lib/run-filters'
import SendReportDialog from './SendReportDialog'
import OverviewTab from './run-detail/OverviewTab'
import PipelineTab from './run-detail/PipelineTab'
import StateTab from './run-detail/StateTab'
import AIAnalysisPanel from './run-detail/AIAnalysisPanel'
import CorrelationPanel from './run-detail/CorrelationPanel'
import CliLogViewer from './CliLogViewer'
import { useFixPrompt } from './run-detail/FixPrompt'

const TABS = ['Overview', 'Pipeline', 'AI Analysis', 'Correlations', 'State', 'Logs'] as const
type Tab = typeof TABS[number]

function LogsTab({ runId }: { runId: string }) {
  const [log, setLog] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    fetch(`/api/logs/${runId}`, { cache: 'no-store' })
      .then((r) => r.ok ? r.text() : null)
      .then((text) => { setLog(text); setLoading(false) })
      .catch(() => setLoading(false))
  }, [runId])

  return (
    <div className="wc" style={{ paddingTop: 22 }}>
      {loading ? (
        <p className="note-line" style={{ margin: 0 }}>Loading log…</p>
      ) : !log ? (
        <p className="note-line" style={{ margin: 0 }}>No log was captured for this run.</p>
      ) : (
        <CliLogViewer log={log} runId={runId} />
      )}
    </div>
  )
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 320, fontSize: 13, color: 'var(--ink-3)' }}>
      {children}
    </div>
  )
}

export default function RunDetailPanel({
  runId,
  allRuns,
}: {
  runId: string | null
  previousRunId?: string | null
  onClose?: () => void
  allRuns: RunSummary[]
  isOverlay?: boolean
}) {
  const { run, loading, error } = useRunDetail(runId)
  const { openRun, serving } = useWorkspace()
  const [activeTab, setActiveTab] = useState<Tab>('Overview')
  const [showReport, setShowReport] = useState(false)
  const fix = useFixPrompt(runId ?? '', undefined, { autoload: true })

  useEffect(() => { setActiveTab('Overview') }, [runId])

  if (!runId) return <Centered>Select a run.</Centered>
  if (loading && !run) return <Centered>Loading run…</Centered>
  if (error || !run) return <Centered><span style={{ color: 'var(--tool)' }}>{error ?? 'Run not found'}</span></Centered>

  const steps = run.steps ?? []
  const reached = steps.filter((s) => s.status !== 'skipped').length
  const summary = allRuns.find((r) => r.run_id === run.run_id)
  const pipeline = summary ? pipelineLabel(summary) : null
  const alias = summary?.alias
  const findings = (run.findings ?? []).filter((f) => !f.suppressed).length

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(run, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${run.run_id}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  const counts: Partial<Record<Tab, number>> = {
    Overview: findings || undefined,
    Pipeline: steps.length || undefined,
  }

  return (
    <div className="panel-slide-in">
      <div className="ws-head">
        <div className="ws-head-row">
          <span className="ws-title">{alias ?? run.run_id}</span>
          <span className={`stat ${toneFor(run.overall_status)}`}><i />{statusWord(run.overall_status)}</span>
          {run.dry_run && <span className="stat mute"><i />dry run</span>}
          <span style={{ flex: 1 }} />
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setShowReport(true)}>Report issue</button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={exportJson}>Export</button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setActiveTab('Pipeline')}>Replay</button>
          {((run.findings ?? []).some((f) => !f.suppressed) || run.overall_status !== 'clean') && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => { setActiveTab('Overview'); void fix.copy() }}
            >
              {fix.label}
            </button>
          )}
        </div>
        <div className="ws-sub">
          {alias && <span className="m">{run.run_id}</span>}
          {alias && <span>·</span>}
          <span>Argus v{run.argus_version}</span>
          {pipeline && <><span>·</span><span>{pipeline}</span></>}
          <span>·</span>
          <span>{reached} of {steps.length} steps</span>
          <span>·</span>
          <span className="m">{formatDuration(run.duration_ms)}</span>
          <span>·</span>
          <span className="m">{fmtClock(run.started_at)}</span>
          {run.parent_run_id && (
            <>
              <span>·</span>
              <span>
                replay of{' '}
                <a
                  href={`/?run=${encodeURIComponent(run.parent_run_id)}`}
                  className="m"
                  style={{ color: 'var(--iris-bright)', textDecoration: 'none' }}
                  onClick={(e) => { e.preventDefault(); openRun(run.parent_run_id!) }}
                >
                  {shortRunId(run.parent_run_id)}
                </a>
                {run.replay_from_step && <> from <span className="m">{run.replay_from_step}</span></>}
              </span>
            </>
          )}
          {serving?.project_root && <><span>·</span><span className="m" title={serving.runs_dir}>{serving.project_root.split('/').pop()}</span></>}
        </div>
        <div className="tabs" role="tablist" aria-label="Run detail sections">
          {TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              role="tab"
              aria-selected={activeTab === tab}
              onClick={() => setActiveTab(tab)}
            >
              {tab}
              {counts[tab] != null && <span className="n">{counts[tab]}</span>}
            </button>
          ))}
        </div>
      </div>

      {activeTab === 'Overview' && <OverviewTab run={run} allRuns={allRuns} onSwitchTab={setActiveTab} fix={fix} />}
      {activeTab === 'Pipeline' && <PipelineTab run={run} />}
      {activeTab === 'AI Analysis' && <div className="wc" style={{ paddingTop: 22 }}><AIAnalysisPanel run={run} /></div>}
      {activeTab === 'Correlations' && <div className="wc" style={{ paddingTop: 22 }}><CorrelationPanel run={run} /></div>}
      {activeTab === 'State' && <StateTab run={run} />}
      {activeTab === 'Logs' && <LogsTab runId={run.run_id} />}

      <SendReportDialog open={showReport} onClose={() => setShowReport(false)} runId={run.run_id} />
    </div>
  )
}
