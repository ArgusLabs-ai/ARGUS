'use client'

import { useMemo, useState, useRef } from 'react'
import { cn } from '@/lib/utils'
import { useSearch, useServingInfo } from '@/lib/hooks'
import { RunStatusBadge } from '@/components/StatusBadge'
import EmptyRunsState from '@/components/EmptyRunsState'
import type { RunSummary, RunStatus } from '@/lib/types'
import {
  ChevronRight,
  RefreshCw,
  Search,
  Pencil,
  Trash2,
  Repeat,
  Check,
  X,
} from 'lucide-react'

/* ── Filter config ─────────────────────────────────────────────── */

type Filter = 'all' | RunStatus

const filters: { id: Filter; label: string }[] = [
  { id: 'all',            label: 'All' },
  { id: 'clean',          label: 'Clean' },
  { id: 'crashed',        label: 'Failed' },
  { id: 'semantic_fail',  label: 'Semantic' },
  { id: 'interrupted',    label: 'Interrupted' },
]

/* ── Format helpers ────────────────────────────────────────────── */

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '\u2014'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const secs = Math.floor(diff / 1000)
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function shortId(id: string): string {
  return id.length > 12 ? id.slice(0, 8) : id
}

/* ── Node dots — show per-step status as colored dots ──────────── */

const DOT_GREEN  = '#22c55e'
const DOT_RED    = '#ef4444'
const DOT_AMBER  = '#eab308'
const DOT_PURPLE = '#a855f7'
const DOT_GRAY   = 'rgba(139,143,160,0.20)'

function inferDotColors(
  nodeNames: string[],
  stepCount: number,
  status: RunStatus,
  firstFailure: string | null,
): string[] {
  const total = nodeNames.length || stepCount
  const completed = Math.min(stepCount, total)
  const dots: string[] = []

  const failIdx = firstFailure ? nodeNames.indexOf(firstFailure) : -1

  for (let i = 0; i < total; i++) {
    if (i >= completed) {
      dots.push(DOT_GRAY)
    } else if (status === 'clean') {
      dots.push(DOT_GREEN)
    } else if (failIdx >= 0 && i === failIdx) {
      dots.push(status === 'semantic_fail' ? DOT_PURPLE : DOT_RED)
    } else if (failIdx >= 0 && i > failIdx) {
      dots.push(DOT_AMBER)
    } else {
      dots.push(DOT_GREEN)
    }
  }
  return dots
}

function NodeDots({ run }: { run: RunSummary }) {
  const total = run.graph_node_names.length || run.step_count
  const completed = run.step_count
  const dots = inferDotColors(
    run.graph_node_names,
    run.step_count,
    run.overall_status,
    run.first_failure_step ?? null,
  )

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-[5px]">
        {dots.slice(0, 10).map((color, i) => (
          <span
            key={i}
            title={run.graph_node_names[i] ?? `step ${i + 1}`}
            className="h-[7px] w-[7px] rounded-full"
            style={{
              background: color,
              boxShadow: color === DOT_RED ? `0 0 6px ${DOT_RED}80` : undefined,
            }}
          />
        ))}
      </div>
      <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {completed}/{total}
      </span>
    </div>
  )
}

/* ── Main Component ────────────────────────────────────────────── */

export default function RunListPanel({
  runs,
  selectedRunId,
  onSelectRun,
  loading,
}: {
  runs: RunSummary[]
  selectedRunId: string | null
  onSelectRun: (id: string) => void
  loading: boolean
}) {
  const [filter, setFilter] = useState<Filter>('all')
  const [searchQuery, setSearchQuery] = useState('')
  const [editingAlias, setEditingAlias] = useState<string | null>(null)
  const [aliasValue, setAliasValue] = useState('')
  const [aliases, setAliases] = useState<Record<string, string>>({})
  const renameRef = useRef<HTMLInputElement>(null)
  const serving = useServingInfo()

  // Filter & search
  const searchResults = useSearch(runs, searchQuery)
  const filteredRuns = useMemo(() => {
    if (filter === 'all') return searchResults
    if (filter === 'crashed') {
      return searchResults.filter((r) => r.overall_status === 'crashed' || r.overall_status === 'silent_failure')
    }
    return searchResults.filter((r) => r.overall_status === filter)
  }, [searchResults, filter])

  // Separate replay children
  const { topLevel, replayChildren } = useMemo(() => {
    const childMap = new Map<string, RunSummary[]>()
    const top: RunSummary[] = []
    for (const r of filteredRuns) {
      if (r.parent_run_id) {
        const existing = childMap.get(r.parent_run_id) ?? []
        existing.push(r)
        childMap.set(r.parent_run_id, existing)
      } else {
        top.push(r)
      }
    }
    return { topLevel: top, replayChildren: childMap }
  }, [filteredRuns])

  // Counts
  const counts = useMemo(() => ({
    total: runs.length,
    failed: runs.filter((r) => r.overall_status === 'crashed' || r.overall_status === 'silent_failure').length,
    clean: runs.filter((r) => r.overall_status === 'clean').length,
  }), [runs])

  // Rename handlers
  const startRename = (runId: string, currentAlias: string | null | undefined) => {
    setEditingAlias(runId)
    setAliasValue(currentAlias ?? '')
    setTimeout(() => renameRef.current?.focus(), 50)
  }

  const saveRename = (runId: string) => {
    if (aliasValue.trim()) {
      setAliases((prev) => ({ ...prev, [runId]: aliasValue.trim() }))
      fetch(`/api/runs/${runId}/alias`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alias: aliasValue.trim() }),
      }).catch(() => {})
    }
    setEditingAlias(null)
  }

  const deleteRun = (runId: string) => {
    fetch(`/api/runs/${runId}`, { method: 'DELETE' }).catch(() => {})
    window.location.reload()
  }

  return (
    <div className="flex h-full flex-col">
      {/* ── Header ──────────────────────────────────────────────── */}
      <header className="flex items-center justify-between border-b border-border px-6 py-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">Runs</h1>
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            {counts.total} runs{' \u00b7 '}
            <button
              onClick={() => setFilter('clean')}
              className="text-success hover:underline"
              title="Show only clean runs"
            >
              {counts.clean} clean
            </button>{' \u00b7 '}
            <button
              onClick={() => setFilter('crashed')}
              className="text-destructive hover:underline"
              title="Show only failed runs (crashed + silent failures)"
            >
              {counts.failed} failed
            </button>
          </p>
          {serving?.runs_dir && (
            <p className="mt-0.5 font-mono text-[11px] text-muted-foreground/70 break-all">
              serving {serving.runs_dir}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => window.location.reload()}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
        </div>
      </header>

      {/* ── Content ─────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto">
        <div className="p-6">
          {/* Search + Filter tabs */}
          <div className="mb-4 flex items-center justify-between gap-4">
            <div className="flex items-center gap-1">
              {filters.map((f) => (
                <button
                  key={f.id}
                  onClick={() => setFilter(f.id)}
                  className={cn(
                    'rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                    filter === f.id
                      ? 'bg-secondary text-foreground'
                      : 'text-muted-foreground hover:bg-muted/50 hover:text-foreground',
                  )}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search runs..."
                className="h-8 w-56 rounded-md border border-border bg-background pl-8 pr-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-ring focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>
          </div>

          {/* ── Grid table ──────────────────────────────────────── */}
          <div className="overflow-hidden rounded-xl border border-border bg-card">
            {!loading && runs.length === 0 ? (
              <EmptyRunsState serving={serving} />
            ) : (
              <>
            {/* Header row */}
            <div className="grid grid-cols-[1.6fr_0.9fr_1.1fr_0.8fr_0.7fr_0.4fr] items-center gap-4 border-b border-border bg-muted/30 px-4 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              <span>Run</span>
              <span>Status</span>
              <span>Steps</span>
              <span>Duration</span>
              <span className="text-right">Tokens</span>
              <span />
            </div>

            {/* Run rows */}
            {loading && !runs.length ? (
              <div className="flex items-center justify-center py-12">
                <span className="text-sm text-muted-foreground">Loading runs...</span>
              </div>
            ) : (
              <ul>
                {topLevel.map((run) => {
                  const isSelected = run.run_id === selectedRunId
                  const displayName = aliases[run.run_id] ?? run.alias ?? null
                  const children = replayChildren.get(run.run_id)

                  return (
                    <li key={run.run_id}>
                      {/* Main run row */}
                      <button
                        onClick={() => onSelectRun(run.run_id)}
                        className={cn(
                          'group grid w-full grid-cols-[1.6fr_0.9fr_1.1fr_0.8fr_0.7fr_0.4fr] items-center gap-4 border-b border-border/60 px-4 py-3 text-left transition-colors last:border-b-0',
                          isSelected ? 'bg-primary/[0.07]' : 'hover:bg-muted/40',
                        )}
                      >
                        {/* Run identity */}
                        <div className="flex min-w-0 items-center gap-3">
                          <span
                            className={cn(
                              'h-8 w-0.5 shrink-0 rounded-full',
                              isSelected ? 'bg-primary' : 'bg-transparent',
                            )}
                          />
                          <div className="min-w-0">
                            {editingAlias === run.run_id ? (
                              <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                                <input
                                  ref={renameRef}
                                  value={aliasValue}
                                  onChange={(e) => setAliasValue(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') saveRename(run.run_id)
                                    if (e.key === 'Escape') setEditingAlias(null)
                                  }}
                                  className="h-6 w-32 rounded border border-ring bg-background px-1.5 text-sm text-foreground focus:outline-none"
                                />
                                <button onClick={() => saveRename(run.run_id)} className="text-success"><Check className="h-3.5 w-3.5" /></button>
                                <button onClick={() => setEditingAlias(null)} className="text-muted-foreground"><X className="h-3.5 w-3.5" /></button>
                              </div>
                            ) : (
                              <p className="truncate text-sm font-medium text-foreground">
                                {displayName ?? run.graph_node_names?.join(' \u2192 ') ?? run.run_id}
                              </p>
                            )}
                            <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                              <span className="font-mono">{shortId(run.run_id)}</span>
                              {run.first_failure_step && (
                                <>
                                  <span className="text-border">{'\u00b7'}</span>
                                  <span className="text-destructive">{run.first_failure_step}</span>
                                </>
                              )}
                            </div>
                          </div>
                        </div>

                        {/* Status */}
                        <div>
                          <RunStatusBadge status={run.overall_status} size="sm" />
                        </div>

                        {/* Steps (node dots) */}
                        <NodeDots run={run} />

                        {/* Duration */}
                        <div className="min-w-0">
                          <p className="font-mono text-sm tabular-nums text-foreground">
                            {formatDuration(run.duration_ms)}
                          </p>
                          <p className="text-[11px] text-muted-foreground">
                            {formatRelative(run.started_at)}
                          </p>
                        </div>

                        {/* Tokens */}
                        <div className="text-right">
                          <p className="font-mono text-sm tabular-nums text-foreground">
                            {'\u2014'}
                          </p>
                        </div>

                        {/* Chevron + actions */}
                        <div className="flex items-center justify-end gap-1">
                          <button
                            onClick={(e) => { e.stopPropagation(); startRename(run.run_id, displayName) }}
                            className="hidden rounded p-1 text-muted-foreground/40 transition-colors hover:text-foreground group-hover:block"
                            title="Rename"
                          >
                            <Pencil className="h-3 w-3" />
                          </button>
                          <button
                            onClick={(e) => { e.stopPropagation(); deleteRun(run.run_id) }}
                            className="hidden rounded p-1 text-muted-foreground/40 transition-colors hover:text-destructive group-hover:block"
                            title="Delete"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                          <ChevronRight
                            className={cn(
                              'h-4 w-4 transition-colors',
                              isSelected
                                ? 'text-primary'
                                : 'text-muted-foreground/40 group-hover:text-muted-foreground',
                            )}
                          />
                        </div>
                      </button>

                      {/* Replay children */}
                      {children?.map((child) => (
                        <button
                          key={child.run_id}
                          onClick={() => onSelectRun(child.run_id)}
                          className={cn(
                            'group grid w-full grid-cols-[1.6fr_0.9fr_1.1fr_0.8fr_0.7fr_0.4fr] items-center gap-4 border-b border-border/60 px-4 py-2.5 pl-8 text-left transition-colors last:border-b-0',
                            child.run_id === selectedRunId ? 'bg-primary/[0.07]' : 'hover:bg-muted/40',
                          )}
                        >
                          <div className="flex min-w-0 items-center gap-3">
                            <Repeat className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
                            <div className="min-w-0">
                              <p className="truncate text-sm text-muted-foreground">
                                Replay{child.replay_from_step ? ` from ${child.replay_from_step}` : ''}
                              </p>
                              <span className="font-mono text-[11px] text-muted-foreground/60">
                                {shortId(child.run_id)}
                              </span>
                            </div>
                          </div>
                          <div><RunStatusBadge status={child.overall_status} size="sm" /></div>
                          <NodeDots run={child} />
                          <div className="min-w-0">
                            <p className="font-mono text-sm tabular-nums text-foreground">
                              {formatDuration(child.duration_ms)}
                            </p>
                            <p className="text-[11px] text-muted-foreground">{formatRelative(child.started_at)}</p>
                          </div>
                          <div className="text-right">
                            <p className="font-mono text-sm tabular-nums text-foreground">{'\u2014'}</p>
                          </div>
                          <div className="flex justify-end">
                            <ChevronRight
                              className={cn(
                                'h-4 w-4 transition-colors',
                                child.run_id === selectedRunId
                                  ? 'text-primary'
                                  : 'text-muted-foreground/40 group-hover:text-muted-foreground',
                              )}
                            />
                          </div>
                        </button>
                      ))}
                    </li>
                  )
                })}
              </ul>
            )}

            {!loading && filteredRuns.length === 0 && runs.length > 0 && (
              <p className="py-12 text-center text-sm text-muted-foreground">
                No runs match this filter.
              </p>
            )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
