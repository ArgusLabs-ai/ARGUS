'use client'

import type { RunSummary, RunRecord } from '@/lib/types'
import { STATUS_DOT_COLOR } from './lib/compare-utils'

const STATUS_DISPLAY: Record<string, string> = {
  clean: 'clean',
  silent_failure: 'silent failure',
  crashed: 'crashed',
  semantic_fail: 'semantic fail',
  interrupted: 'interrupted',
}

function statusColor(s: string) {
  return STATUS_DOT_COLOR[s] ?? 'var(--ink-3)'
}

function selectorLabel(run: RunRecord | null, fallback: string): string {
  if (!run) return fallback
  const status = run.overall_status
  const isReplay = !!run.parent_run_id
  if (isReplay) {
    const fixedCrash = status === 'clean' || status === 'silent_failure'
    return `Rerun${run.run_id.includes('-R') ? ` ${run.run_id.split('-R').pop()}` : ''} (${fixedCrash ? 'Fixed' : 'Failed'})`
  }
  const label = status === 'clean' ? 'Passed' : status === 'crashed' ? 'Failed' : status === 'silent_failure' ? 'Failed' : 'Unknown'
  return `Base Run (${label})`
}

function badgeLabel(run: RunRecord): string {
  const isReplay = !!run.parent_run_id
  if (isReplay) {
    const base = STATUS_DISPLAY[run.overall_status] ?? run.overall_status
    return `rerun (fixed ${base === 'clean' ? 'all' : base})`
  }
  return STATUS_DISPLAY[run.overall_status] ?? run.overall_status
}

const STATUS_DOT_SYMBOL: Record<string, string> = {
  clean: '\u{1F7E9}',          // green square (smaller than circle emoji)
  silent_failure: '\u{1F7E8}', // yellow square
  crashed: '\u{1F7E5}',        // red square
  semantic_fail: '\u{1F7EA}',  // purple square
  interrupted: '\u{1F7E8}',    // yellow square
}

function statusDot(s: string) { return STATUS_DOT_SYMBOL[s] ?? '\u2B1C' }

function RunSelectOptions({ runs }: { runs: RunSummary[] }) {
  // Group: parent runs first, then their replays indented beneath
  const parents = runs.filter((r) => !r.parent_run_id)
  const childMap = new Map<string, RunSummary[]>()
  for (const r of runs) {
    if (r.parent_run_id) {
      const list = childMap.get(r.parent_run_id) ?? []
      list.push(r)
      childMap.set(r.parent_run_id, list)
    }
  }
  const options: JSX.Element[] = []
  for (const p of parents) {
    const label = p.alias || p.run_id
    options.push(
      <option key={p.run_id} value={p.run_id}>
        {statusDot(p.overall_status)} {label}
      </option>
    )
    const children = childMap.get(p.run_id) ?? []
    for (const c of children) {
      const cName = c.alias || c.run_id.slice(0, 12)
      const from = c.replay_from_step ? ` from ${c.replay_from_step}` : ''
      options.push(
        <option key={c.run_id} value={c.run_id}>
          {`  \u21B3 ${statusDot(c.overall_status)} ${cName}${from}`}
        </option>
      )
    }
  }
  return <>{options}</>
}

export default function CompareHeader({
  runs,
  selectedA,
  selectedB,
  runA,
  runB,
  onSelectA,
  onSelectB,
}: {
  runs: RunSummary[]
  selectedA: string
  selectedB: string
  runA: RunRecord | null
  runB: RunRecord | null
  onSelectA: (id: string) => void
  onSelectB: (id: string) => void
}) {
  // Build alias lookup from the runs list
  const aliasMap = new Map<string, string>()
  for (const r of runs) {
    if (r.alias) aliasMap.set(r.run_id, r.alias)
  }
  function displayName(run: RunRecord): string {
    return aliasMap.get(run.run_id) || run.run_id.slice(0, 15)
  }

  return (
    <div className="space-y-2.5">
      {/* Selectors row */}
      <div className="flex items-center gap-2.5 flex-wrap">
        <select
          value={selectedA}
          onChange={(e) => onSelectA(e.target.value)}
          className="text-[13px] px-3.5 py-2 rounded-lg outline-none"
          style={{
            background: 'var(--card)',
            border: '1px solid var(--border)',
            color: 'var(--foreground)',
            minWidth: '200px',
          }}
        >
          <option value="">{selectorLabel(runA, 'Select base run...')}</option>
          <RunSelectOptions runs={runs} />
        </select>

        {/* Swap / Copy */}
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => { onSelectA(selectedB); onSelectB(selectedA) }}
            className="w-8 h-8 rounded-lg flex items-center justify-center hover:opacity-80 transition-opacity"
            style={{ color: 'var(--text-tertiary)', border: '1px solid var(--border)' }}
            title="Swap runs"
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M4 3l-2.5 2.5L4 8M10 6l2.5 2.5L10 11" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M1.5 5.5h11M12.5 8.5h-11" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
            </svg>
          </button>
          <button
            type="button"
            className="w-8 h-8 rounded-lg flex items-center justify-center hover:opacity-80 transition-opacity"
            style={{ color: 'var(--text-tertiary)', border: '1px solid var(--border)' }}
            title="Copy comparison URL"
            onClick={() => navigator.clipboard?.writeText(window.location.href)}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <rect x="4" y="4" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.2"/>
              <path d="M10 4V3a1.5 1.5 0 00-1.5-1.5H3A1.5 1.5 0 001.5 3v5.5A1.5 1.5 0 003 10h1" stroke="currentColor" strokeWidth="1.2"/>
            </svg>
          </button>
        </div>

        <select
          value={selectedB}
          onChange={(e) => onSelectB(e.target.value)}
          className="text-[13px] px-3.5 py-2 rounded-lg outline-none"
          style={{
            background: 'var(--card)',
            border: '1px solid var(--border)',
            color: 'var(--foreground)',
            minWidth: '200px',
          }}
        >
          <option value="">{selectorLabel(runB, 'Select comparison run...')}</option>
          <RunSelectOptions runs={runs} />
        </select>

        <div className="flex-1" />

        {/* Action buttons */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="text-[12px] font-medium px-3 py-1.5 rounded-lg flex items-center gap-1.5 hover:opacity-80 transition-opacity"
            style={{ color: 'var(--text-secondary)', border: '1px solid var(--border)' }}
          >
            <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
              <path d="M4.5 2.5v-1a1 1 0 011-1h2a1 1 0 011 1v1M2 4.5h9M8.5 6v4M4.5 6v4M3 4.5l.5 6a1.5 1.5 0 001.5 1.5h3a1.5 1.5 0 001.5-1.5l.5-6" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            Share
          </button>
          <button
            type="button"
            className="text-[12px] font-medium px-3 py-1.5 rounded-lg flex items-center gap-1.5 hover:opacity-80 transition-opacity"
            style={{ color: 'var(--text-secondary)', border: '1px solid var(--border)' }}
          >
            <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
              <path d="M6.5 8.5V1.5M3.5 5.5l3 3 3-3" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M1.5 9v2a1 1 0 001 1h8a1 1 0 001-1V9" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            Export
          </button>
          <button
            type="button"
            className="text-[12px] font-medium px-3 py-1.5 rounded-lg flex items-center gap-1.5 hover:opacity-80 transition-opacity"
            style={{ color: 'var(--primary)', border: '1px solid var(--primary)' }}
          >
            + Rerun from diff
          </button>
        </div>
      </div>

      {/* Run info cards */}
      {runA && runB && (
        <div className="flex items-center gap-3">
          {/* Run A card */}
          <div
            className="flex-1 px-4 py-3 rounded-xl"
            style={{ background: 'var(--card)', border: '1px solid var(--border)' }}
          >
            <div className="flex items-center gap-2.5">
              <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: statusColor(runA.overall_status) }} />
              <span className="text-[13.5px] font-bold tracking-[-0.02em]" style={{ color: 'var(--foreground)' }}>
                {displayName(runA)}
              </span>
              {aliasMap.has(runA.run_id) && (
                <span className="text-[10px] font-mono" style={{ color: 'var(--text-tertiary)' }}>{runA.run_id.slice(0, 8)}</span>
              )}
              <span
                className="text-[10.5px] font-semibold px-2 py-0.5 rounded-md"
                style={{ color: statusColor(runA.overall_status), background: `color-mix(in srgb, ${statusColor(runA.overall_status)} 10%, transparent)` }}
              >
                {badgeLabel(runA)}
              </span>
            </div>
          </div>

          {/* VS */}
          <span className="text-[13px] font-bold shrink-0" style={{ color: 'var(--text-tertiary)' }}>vs</span>

          {/* Run B card */}
          <div
            className="flex-1 px-4 py-3 rounded-xl"
            style={{ background: 'var(--card)', border: '1px solid var(--border)' }}
          >
            <div className="flex items-center gap-2.5">
              <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: statusColor(runB.overall_status) }} />
              <span className="text-[13.5px] font-bold tracking-[-0.02em]" style={{ color: 'var(--foreground)' }}>
                {displayName(runB)}
              </span>
              {aliasMap.has(runB.run_id) && (
                <span className="text-[10px] font-mono" style={{ color: 'var(--text-tertiary)' }}>{runB.run_id.slice(0, 8)}</span>
              )}
              <span
                className="text-[10.5px] font-semibold px-2 py-0.5 rounded-md"
                style={{ color: statusColor(runB.overall_status), background: `color-mix(in srgb, ${statusColor(runB.overall_status)} 10%, transparent)` }}
              >
                {badgeLabel(runB)}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
