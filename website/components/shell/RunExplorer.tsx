'use client'

/* Run Explorer — the grouped run list in the IDE frame's second column.
   Group headers are real disclosure buttons on a banded ground; items are
   one step lighter. Selection is an iris tint, never a border. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useRouter } from 'next/navigation'
import { Search, RotateCw, File, CornerDownRight, Radio, Workflow, Bookmark } from 'lucide-react'
import { useWorkspace, shortRunId, toneFor, relativeAge } from '@/lib/workspace'
import { pipelineKey, pipelineLabel, pipelineNodes } from '@/lib/run-filters'
import type { RunSummary } from '@/lib/types'
import { cn } from '@/lib/utils'

const FAILING = new Set(['crashed', 'silent_failure', 'semantic_fail'])
const RECENT_N = 12
const FAILING_N = 8

const SAVED_VIEWS: { label: string; href: string }[] = [
  { label: 'Crashed', href: '/?status=crashed' },
  { label: 'Silent failures', href: '/?status=silent_failure' },
  { label: 'Semantic fails', href: '/?status=semantic_fail' },
  { label: 'Clean', href: '/?status=clean' },
]

function Group({
  title,
  count,
  action,
  defaultOpen = true,
  children,
}: {
  title: string
  count?: number | string
  action?: ReactNode
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  const id = `exg-${title.replace(/\W+/g, '-').toLowerCase()}`
  return (
    <div className="ex-group">
      <button
        type="button"
        className={cn('ex-head', !open && 'closed')}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="tri" />
        {title}
        {action ?? (count !== undefined && <span className="n">{count}</span>)}
      </button>
      {open && <div className="ex-body" id={id}>{children}</div>}
    </div>
  )
}

function RunItem({
  run,
  active,
  nest,
  onOpen,
}: {
  run: RunSummary
  active: boolean
  nest?: boolean
  onOpen: (id: string) => void
}) {
  const tone = toneFor(run.overall_status)
  const label = run.alias ?? (nest ? `replay · ${run.replay_from_step ?? shortRunId(run.run_id)}` : shortRunId(run.run_id))
  return (
    <button
      type="button"
      className={cn('ex-item', active && 'on', nest && 'nest')}
      onClick={() => onOpen(run.run_id)}
      title={run.run_id}
      aria-current={active ? 'true' : undefined}
    >
      {nest ? <CornerDownRight /> : run.overall_status === 'interrupted' ? <Radio className={cn('fg', tone)} /> : <File className={cn('fg', tone)} />}
      <span className="lb">{label}</span>
      <span className="ago">{relativeAge(run.started_at)}</span>
    </button>
  )
}

export default function RunExplorer() {
  const router = useRouter()
  const { runs, runsLoading, refreshRuns, activeRunId, openRun } = useWorkspace()
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  /* ⌘K / Ctrl+K focuses the explorer search; Escape clears it. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setSearching(true)
        setTimeout(() => inputRef.current?.focus(), 0)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const q = query.trim().toLowerCase()
  const matches = useMemo(() => {
    if (!q) return null
    return runs.filter((r) =>
      r.run_id.toLowerCase().includes(q) ||
      (r.alias ?? '').toLowerCase().includes(q) ||
      r.overall_status.includes(q) ||
      (r.first_failure_step ?? '').toLowerCase().includes(q) ||
      r.graph_node_names.some((n) => n.toLowerCase().includes(q)),
    ).slice(0, 40)
  }, [runs, q])

  const failing = useMemo(() => runs.filter((r) => FAILING.has(r.overall_status)), [runs])

  const { recent, children } = useMemo(() => {
    const childMap = new Map<string, RunSummary[]>()
    for (const r of runs) {
      if (r.parent_run_id) {
        const list = childMap.get(r.parent_run_id) ?? []
        list.push(r)
        childMap.set(r.parent_run_id, list)
      }
    }
    const top = runs.filter((r) => !r.parent_run_id).slice(0, RECENT_N)
    return { recent: top, children: childMap }
  }, [runs])

  const pipelines = useMemo(() => {
    const seen = new Map<string, { key: string; label: string; nodes: number; count: number }>()
    for (const r of runs) {
      const key = pipelineKey(r)
      const cur = seen.get(key)
      if (cur) cur.count += 1
      else seen.set(key, { key, label: pipelineLabel(r), nodes: pipelineNodes(r).length, count: 1 })
    }
    return Array.from(seen.values()).sort((a, b) => b.count - a.count).slice(0, 8)
  }, [runs])

  const open = (id: string) => openRun(id)

  return (
    <aside className="explorer" aria-label="Run explorer">
      {searching ? (
        <div className="ex-search" style={{ cursor: 'text' }}>
          <Search />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') { setQuery(''); setSearching(false) }
            }}
            onBlur={() => { if (!query) setSearching(false) }}
            placeholder={`Search ${runs.length.toLocaleString()} runs`}
            aria-label="Search runs"
            style={{
              flex: 1, minWidth: 0, border: 'none', background: 'transparent', outline: 'none',
              font: 'inherit', color: 'var(--ink)', padding: 0,
            }}
          />
          <span className="kbd">esc</span>
        </div>
      ) : (
        <button type="button" className="ex-search" onClick={() => { setSearching(true); setTimeout(() => inputRef.current?.focus(), 0) }}>
          <Search />
          Search runs
          <span className="kbd">⌘K</span>
        </button>
      )}

      <div className="ex-title">
        Run Explorer
        <button
          type="button"
          onClick={refreshRuns}
          title="Refresh"
          aria-label="Refresh runs"
          style={{ border: 'none', background: 'transparent', padding: 2, cursor: 'pointer', display: 'flex', color: 'var(--ink-4)' }}
        >
          <RotateCw style={{ width: 13, height: 13 }} className={runsLoading ? 'animate-soft-pulse' : undefined} />
        </button>
      </div>

      <div className="ex-scroll">
        {matches ? (
          <Group title="Results" count={matches.length}>
            {matches.length === 0 && <div className="ex-empty">No runs match “{query}”.</div>}
            {matches.map((r) => (
              <RunItem key={r.run_id} run={r} active={r.run_id === activeRunId} onOpen={open} />
            ))}
          </Group>
        ) : (
          <>
            <Group title="Failing now" count={failing.length}>
              {failing.length === 0 && (
                <div className="ex-empty">{runsLoading ? 'Loading…' : 'Nothing failing.'}</div>
              )}
              {failing.slice(0, FAILING_N).map((r) => (
                <RunItem key={r.run_id} run={r} active={r.run_id === activeRunId} onOpen={open} />
              ))}
              {failing.length > FAILING_N && (
                <button
                  type="button"
                  className="ex-item"
                  onClick={() => router.push('/?status=crashed&status=silent_failure&status=semantic_fail')}
                >
                  <span className="fg" />
                  <span className="lb" style={{ fontFamily: 'var(--sans)', color: 'var(--ink-3)' }}>
                    All {failing.length.toLocaleString()} failing…
                  </span>
                </button>
              )}
            </Group>

            <Group title="Recent" count={runs.length}>
              {recent.length === 0 && (
                <div className="ex-empty">{runsLoading ? 'Loading…' : 'No runs yet.'}</div>
              )}
              {recent.map((r) => (
                <div key={r.run_id}>
                  <RunItem run={r} active={r.run_id === activeRunId} onOpen={open} />
                  {children.get(r.run_id)?.map((c) => (
                    <RunItem key={c.run_id} run={c} nest active={c.run_id === activeRunId} onOpen={open} />
                  ))}
                </div>
              ))}
            </Group>

            <Group title="Pipelines" count={pipelines.length}>
              {pipelines.length === 0 && <div className="ex-empty">—</div>}
              {pipelines.map((p) => (
                <button
                  key={p.key}
                  type="button"
                  className="ex-item"
                  title={`${p.label} · ${p.count} runs`}
                  onClick={() => router.push(`/?pipeline=${p.key}`)}
                >
                  <Workflow className="fg" />
                  <span className="lb">{p.label}</span>
                  <span className="ago">{p.nodes}n</span>
                </button>
              ))}
            </Group>

            <Group title="Saved views" count={SAVED_VIEWS.length} defaultOpen={false}>
              {SAVED_VIEWS.map((v) => (
                <button key={v.href} type="button" className="ex-item" onClick={() => router.push(v.href)}>
                  <Bookmark className="fg" />
                  <span className="lb" style={{ fontFamily: 'var(--sans)' }}>{v.label}</span>
                </button>
              ))}
            </Group>
          </>
        )}
      </div>
    </aside>
  )
}
