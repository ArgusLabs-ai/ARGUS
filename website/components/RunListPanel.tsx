'use client'

/* Runs — the workspace view when no run tab is active. Rows on hairlines,
   a status word with a dot (never a filled badge), the step dots, and a
   flush hotspot matrix under a quiet caption. */

import { useMemo, useRef, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { Pencil, Trash2, Check, X, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useWorkspace, shortRunId, toneFor, statusWord, relativeAge, formatDuration } from '@/lib/workspace'
import EmptyRunsState from '@/components/EmptyRunsState'
import HotspotMatrix from '@/components/HotspotMatrix'
import RunFilterBar from '@/components/RunFilterBar'
import {
  applyFiltersToParams,
  filtersFromSearchParams,
  runMatchesFilters,
  type RunFilter,
} from '@/lib/run-filters'
import type { RunSummary, RunStatus } from '@/lib/types'

/* ── Step dots ─────────────────────────────────────────────────── */

function dotClasses(run: RunSummary): string[] {
  const names = run.graph_node_names ?? []
  const total = names.length || run.step_count
  const completed = Math.min(run.step_count, total)
  const failIdx = run.first_failure_step ? names.indexOf(run.first_failure_step) : -1
  const out: string[] = []
  for (let i = 0; i < total; i++) {
    if (i >= completed) out.push('idle')
    else if (run.overall_status === 'clean') out.push('ok')
    else if (failIdx >= 0 && i === failIdx) out.push(run.overall_status === 'semantic_fail' ? 'sem' : 'bad')
    else if (failIdx >= 0 && i > failIdx) out.push('warn')
    else out.push('ok')
  }
  return out
}

function Dots({ run }: { run: RunSummary }) {
  const dots = dotClasses(run)
  const total = dots.length
  return (
    <span className="dots">
      <i>
        {dots.slice(0, 10).map((c, i) => (
          <b key={i} className={c} title={run.graph_node_names[i] ?? `step ${i + 1}`} />
        ))}
      </i>
      <span>{Math.min(run.step_count, total)}/{total}</span>
    </span>
  )
}

/* ── Quick filters ─────────────────────────────────────────────── */

type Quick = 'all' | 'failing' | 'clean' | 'semantic'
const FAILING: RunStatus[] = ['crashed', 'silent_failure']

function quickOf(filters: RunFilter[]): Quick {
  const statuses = filters.filter((f) => f.key === 'status').map((f) => f.value)
  if (statuses.length === 0) return 'all'
  if (statuses.length === 1 && statuses[0] === 'clean') return 'clean'
  if (statuses.length === 1 && statuses[0] === 'semantic_fail') return 'semantic'
  if (statuses.length === 2 && FAILING.every((s) => statuses.includes(s))) return 'failing'
  return 'all'
}

/* ── Panel ─────────────────────────────────────────────────────── */

export default function RunListPanel({ runs, loading }: { runs: RunSummary[]; loading: boolean }) {
  const router = useRouter()
  const searchParams = useSearchParams()
  const { openRun, serving, refreshRuns } = useWorkspace()
  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])
  const [query, setQuery] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [aliasValue, setAliasValue] = useState('')
  const [aliases, setAliases] = useState<Record<string, string>>({})
  const renameRef = useRef<HTMLInputElement>(null)

  const setFilters = (next: RunFilter[]) => {
    const params = applyFiltersToParams(searchParams, next)
    const qs = params.toString()
    router.replace(qs ? `/?${qs}` : '/', { scroll: false })
  }

  const setQuick = (q: Quick) => {
    const rest = filters.filter((f) => f.key !== 'status')
    if (q === 'all') setFilters(rest)
    else if (q === 'clean') setFilters([...rest, { key: 'status', value: 'clean' }])
    else if (q === 'semantic') setFilters([...rest, { key: 'status', value: 'semantic_fail' }])
    else setFilters([...rest, ...FAILING.map((s) => ({ key: 'status' as const, value: s }))])
  }
  const quick = quickOf(filters)

  const q = query.trim().toLowerCase()
  const visible = useMemo(() => {
    let list = runs.filter((r) => runMatchesFilters(r, filters))
    if (q) {
      list = list.filter((r) =>
        r.run_id.toLowerCase().includes(q) ||
        (r.alias ?? '').toLowerCase().includes(q) ||
        r.overall_status.includes(q) ||
        (r.first_failure_step ?? '').toLowerCase().includes(q) ||
        r.graph_node_names.some((n) => n.toLowerCase().includes(q)),
      )
    }
    return list
  }, [runs, filters, q])

  const { topLevel, children } = useMemo(() => {
    const childMap = new Map<string, RunSummary[]>()
    const top: RunSummary[] = []
    for (const r of visible) {
      if (r.parent_run_id) {
        const list = childMap.get(r.parent_run_id) ?? []
        list.push(r)
        childMap.set(r.parent_run_id, list)
      } else top.push(r)
    }
    return { topLevel: top, children: childMap }
  }, [visible])

  const counts = useMemo(() => ({
    total: runs.length,
    failing: runs.filter((r) => r.overall_status !== 'clean').length,
    clean: runs.filter((r) => r.overall_status === 'clean').length,
  }), [runs])

  const startRename = (id: string, current: string | null | undefined) => {
    setEditing(id)
    setAliasValue(current ?? '')
    setTimeout(() => renameRef.current?.focus(), 30)
  }
  const saveRename = (id: string) => {
    const v = aliasValue.trim()
    if (v) {
      setAliases((prev) => ({ ...prev, [id]: v }))
      fetch(`/api/runs/${id}/alias`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alias: v }),
      }).catch(() => {})
    }
    setEditing(null)
  }
  const deleteRun = async (id: string) => {
    await fetch(`/api/runs/${id}`, { method: 'DELETE' }).catch(() => {})
    refreshRuns()
  }

  if (!loading && runs.length === 0) {
    return (
      <div className="wc">
        <EmptyRunsState serving={serving} />
      </div>
    )
  }

  const Row = ({ run, child }: { run: RunSummary; child?: boolean }) => {
    const name = aliases[run.run_id] ?? run.alias ?? null
    const tone = toneFor(run.overall_status)
    return (
      <div
        role="row"
        tabIndex={0}
        className={cn('rrow', child && 'child')}
        onClick={() => openRun(run.run_id)}
        onKeyDown={(e) => { if (e.key === 'Enter') openRun(run.run_id) }}
      >
        <div style={{ minWidth: 0 }}>
          {editing === run.run_id ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }} onClick={(e) => e.stopPropagation()}>
              <input
                ref={renameRef}
                className="inp"
                style={{ height: 26, width: 180, fontSize: 12.5 }}
                value={aliasValue}
                onChange={(e) => setAliasValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') saveRename(run.run_id)
                  if (e.key === 'Escape') setEditing(null)
                }}
                placeholder="Alias"
              />
              <button type="button" className="btn btn-sm btn-icon btn-ghost" onClick={() => saveRename(run.run_id)} aria-label="Save"><Check /></button>
              <button type="button" className="btn btn-sm btn-icon btn-ghost" onClick={() => setEditing(null)} aria-label="Cancel"><X /></button>
            </div>
          ) : (
            <div className="rrow-name" style={child ? { color: 'var(--ink-2)', fontWeight: 400 } : undefined}>
              {child
                ? <>replay{run.replay_from_step ? <> · <span className="m" style={{ fontFamily: 'var(--mono)' }}>{run.replay_from_step}</span></> : null}</>
                : name ?? <span style={{ fontFamily: 'var(--mono)' }}>{run.run_id}</span>}
            </div>
          )}
          <div className="rrow-sub">
            <span className="m">{shortRunId(run.run_id)}</span>
            {name && !child && <><span>·</span><span className="m">{run.run_id}</span></>}
            {run.first_failure_step && (
              <>
                <span>·</span>
                <span className="m" style={{ color: tone === 'sem' ? 'var(--semantic)' : tone === 'warn' ? 'var(--quality)' : 'var(--tool)' }}>
                  {run.first_failure_step}
                </span>
              </>
            )}
          </div>
        </div>
        <span className={cn('stat', tone)}><i />{statusWord(run.overall_status)}</span>
        <Dots run={run} />
        <div>
          <div className="rrow-dur">{formatDuration(run.duration_ms)}</div>
        </div>
        <div className="rrow-ago" style={{ marginTop: 0, fontSize: 12 }}>{relativeAge(run.started_at)} ago</div>
        <div className="rrow-act">
          <button type="button" title="Rename" aria-label="Rename" onClick={(e) => { e.stopPropagation(); startRename(run.run_id, name) }}><Pencil /></button>
          <button type="button" title="Delete" aria-label="Delete" className="danger" onClick={(e) => { e.stopPropagation(); void deleteRun(run.run_id) }}><Trash2 /></button>
        </div>
      </div>
    )
  }

  return (
    <div className="panel-slide-in">
      <div className="ws-head">
        <div className="ws-head-row">
          <span className="ws-title" style={{ fontFamily: 'var(--sans)', letterSpacing: '-0.02em' }}>Runs</span>
          <span style={{ flex: 1 }} />
          <div className="inp" style={{ display: 'flex', alignItems: 'center', gap: 8, width: 240, height: 30, padding: '0 9px' }}>
            <Search style={{ width: 13, height: 13, color: 'var(--ink-4)', flex: 'none' }} />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter this list"
              aria-label="Filter runs"
              style={{ border: 'none', outline: 'none', background: 'transparent', font: 'inherit', color: 'var(--ink)', flex: 1, minWidth: 0, padding: 0 }}
            />
          </div>
        </div>
        <div className="ws-sub">
          <span><span className="m">{counts.total.toLocaleString()}</span> runs</span>
          <span>·</span>
          <span><span className="m">{counts.failing.toLocaleString()}</span> failing</span>
          <span>·</span>
          <span><span className="m">{counts.clean.toLocaleString()}</span> clean</span>
          {serving?.runs_dir && (
            <>
              <span>·</span>
              <span className="m" style={{ color: 'var(--ink-4)' }} title={serving.runs_dir}>{serving.runs_dir}</span>
            </>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginTop: 14, paddingBottom: 10, flexWrap: 'wrap' }}>
          <div className="fpills" role="tablist" aria-label="Quick filters">
            {([['all', 'All'], ['failing', 'Failing'], ['semantic', 'Semantic'], ['clean', 'Clean']] as [Quick, string][]).map(([k, label]) => (
              <button key={k} type="button" role="tab" className="fpill" aria-selected={quick === k} onClick={() => setQuick(k)}>
                {label}
              </button>
            ))}
          </div>
          <RunFilterBar runs={runs} filters={filters.filter((f) => f.key !== 'status' || quick === 'all')} onChange={setFilters} allFilters={filters} />
        </div>
      </div>

      <div className="wc" style={{ paddingTop: 26 }}>
        {runs.length > 0 && (
          <HotspotMatrix
            runs={runs}
            onSelectCell={(origin, node) =>
              setFilters([
                ...filters.filter((f) => f.key !== 'origin' && f.key !== 'node'),
                { key: 'origin', value: origin },
                { key: 'node', value: node },
              ])
            }
          />
        )}

        <div>
          <p className="cap">
            <span>
              {visible.length === runs.length
                ? `All runs · ${runs.length.toLocaleString()}`
                : `${visible.length.toLocaleString()} of ${runs.length.toLocaleString()} runs`}
            </span>
          </p>
          <div className="rlist" role="table" aria-label="Runs">
            <div className="rrow head" role="row">
              <span>Run</span>
              <span>Status</span>
              <span>Steps</span>
              <span>Duration</span>
              <span>Started</span>
              <span />
            </div>
            {loading && runs.length === 0 && (
              <div style={{ padding: '40px 0', textAlign: 'center', fontSize: 13, color: 'var(--ink-3)' }}>Loading runs…</div>
            )}
            {topLevel.map((run) => (
              <div key={run.run_id}>
                <Row run={run} />
                {children.get(run.run_id)?.map((c) => <Row key={c.run_id} run={c} child />)}
              </div>
            ))}
            {!loading && visible.length === 0 && runs.length > 0 && (
              <div style={{ padding: '40px 0', textAlign: 'center', fontSize: 13, color: 'var(--ink-3)' }}>
                No runs match this filter.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
