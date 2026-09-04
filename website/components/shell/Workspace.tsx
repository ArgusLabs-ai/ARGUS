'use client'

/* Workspace column: the view-toggle strip, the tab strip of open runs, and
   the body where the active route renders. */

import { useMemo, type ReactNode } from 'react'
import { Radio, Pause, Info, X, File, GitCompareArrows } from 'lucide-react'
import { useWorkspace, shortRunId, toneFor, type WsTab } from '@/lib/workspace'
import { cn } from '@/lib/utils'

function WorkspaceTop() {
  const { live, setLive, serving, note, dismissNote } = useWorkspace()
  const where = useMemo(() => {
    const dir = serving?.runs_dir
    if (!dir) return 'Local'
    const parts = dir.replace(/\/+$/, '').split('/')
    const tail = parts.slice(-2).join('/')
    return `Local · ${tail}`
  }, [serving])

  return (
    <div className="ws-top">
      <div className="ws-vp" role="group" aria-label="Run list updates">
        <button type="button" className={cn(live && 'on')} title="Live tail" aria-pressed={live} onClick={() => setLive(true)}>
          <Radio />
        </button>
        <button type="button" className={cn(!live && 'on')} title="Paused" aria-pressed={!live} onClick={() => setLive(false)}>
          <Pause />
        </button>
      </div>
      <span style={{ fontSize: 11.5, color: 'var(--ink-3)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={serving?.runs_dir}>
        {where} · {live ? 'live' : 'paused'}
      </span>
      {note && (
        <div className="ws-note">
          <Info />
          {note.text}
          <button
            type="button"
            onClick={dismissNote}
            aria-label="Dismiss"
            style={{ border: 'none', background: 'transparent', padding: 0, display: 'flex', cursor: 'pointer', color: 'var(--ink-4)' }}
          >
            <X />
          </button>
        </div>
      )}
    </div>
  )
}

function TabStrip() {
  const { tabs, activeTabId, runs, openRun, openCompare, closeTab } = useWorkspace()
  if (tabs.length === 0) return null

  const statusOf = (id: string) => runs.find((r) => r.run_id === id)?.overall_status
  const labelOf = (t: WsTab) => {
    if (t.kind === 'run') {
      const alias = runs.find((r) => r.run_id === t.runId)?.alias
      return alias ?? shortRunId(t.runId)
    }
    return `${shortRunId(t.a)} ↔ ${shortRunId(t.b)}`
  }
  const activate = (t: WsTab) => (t.kind === 'run' ? openRun(t.runId) : openCompare(t.a, t.b))

  return (
    <div className="tabbar" role="tablist" aria-label="Open runs">
      {tabs.map((t) => {
        const on = t.id === activeTabId
        return (
          <div
            key={t.id}
            role="tab"
            tabIndex={0}
            aria-selected={on}
            className={cn('tab', on && 'on')}
            onClick={() => activate(t)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(t) }
              if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'w') { e.preventDefault(); closeTab(t.id) }
            }}
            title={t.kind === 'run' ? t.runId : `${t.a} ↔ ${t.b}`}
          >
            {t.kind === 'run'
              ? <File className={cn('fg', toneFor(statusOf(t.runId)))} />
              : <GitCompareArrows className="fg" />}
            <span className="lb">{labelOf(t)}</span>
            <button
              type="button"
              className="x"
              aria-label={`Close ${labelOf(t)}`}
              onClick={(e) => { e.stopPropagation(); closeTab(t.id) }}
            >
              <X />
            </button>
          </div>
        )
      })}
    </div>
  )
}

export default function Workspace({ children }: { children: ReactNode }) {
  return (
    <div className="ws">
      <WorkspaceTop />
      <TabStrip />
      <div className="ws-body">{children}</div>
    </div>
  )
}
