'use client'

/* Findings, compartmentalised: one group per node (culprit first, then by
   worst severity), and inside a group one row per flagged field. A row is
   one capsule (its lead signal, "+N" for the rest), the field, and a
   clamped sentence; source and confidence sit in the row's tooltip. The
   first few rows show, the rest fold. "Fix prompt" fetches the real
   `argus fix` markdown and opens it flush under the row. */

import { useMemo, useState } from 'react'
import type { Finding, RunRecord } from '@/lib/types'
import { trimReason } from '@/lib/run-detail'
import { findingMeta } from '@/lib/failure-labels'
import { STATUS_META, mapStatus } from '@/lib/graph-model'
import Prose from './Prose'
import FixPromptButton from './FixPrompt'

const SEV_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 }
const SEV_COLOR: Record<string, string> = { critical: 'var(--tool)', warning: 'var(--quality)', info: 'var(--iris)' }
const FOLD_AT = 5

interface Row { key: string; lead: Finding; all: Finding[] }
interface Group { node: string; rows: Row[]; worst: number; order: number }

function sentence(f: Finding): string {
  const r = trimReason(f.reason, f.node)
  return r ? r[0].toUpperCase() + r.slice(1) : f.reason
}

function groupFindings(findings: Finding[], run: RunRecord, culprit: string | null | undefined): Group[] {
  const order = new Map((run.steps ?? []).map((s, i) => [s.node_name, i]))
  const groups = new Map<string, Group>()
  const sorted = findings.slice().sort((a, b) => (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3))
  for (const f of sorted) {
    const g = groups.get(f.node) ?? { node: f.node, rows: [], worst: 3, order: order.get(f.node) ?? 99 }
    groups.set(f.node, g)
    g.worst = Math.min(g.worst, SEV_RANK[f.severity] ?? 3)
    const key = f.field_path ? `${f.suppressed ? 's' : 'a'}:${f.field_path}` : f.id
    const row = g.rows.find((r) => r.key === key)
    if (row) row.all.push(f)
    else g.rows.push({ key, lead: f, all: [f] })
  }
  return Array.from(groups.values()).sort((a, b) => {
    if (a.node === culprit) return -1
    if (b.node === culprit) return 1
    return a.worst - b.worst || a.order - b.order
  })
}

export default function FindingsPanel({
  findings,
  run,
  onSelectNode,
  culprit,
}: {
  findings: Finding[]
  run: RunRecord
  onSelectNode: (node: string) => void
  culprit?: string | null
}) {
  const [showSuppressed, setShowSuppressed] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const active = findings.filter((f) => !f.suppressed)
  const suppressed = findings.filter((f) => f.suppressed)
  const groups = useMemo(
    () => groupFindings(showSuppressed ? findings : active, run, culprit),
    [findings, active, showSuppressed, run, culprit],
  )
  if (!findings.length) return null

  const crit = active.filter((f) => f.severity === 'critical').length
  const warn = active.filter((f) => f.severity === 'warning').length
  const stepOf = (n: string) => (run.steps ?? []).find((s) => s.node_name === n)

  /* Fold after FOLD_AT rows, keeping whole groups in their order. */
  const totalRows = groups.reduce((k, g) => k + g.rows.length, 0)
  let budget = showAll ? Infinity : FOLD_AT
  const shown = groups
    .map((g) => {
      const rows = g.rows.slice(0, Math.max(0, budget))
      budget -= rows.length
      return { ...g, rows }
    })
    .filter((g) => g.rows.length)

  return (
    <section className="ov-sec">
      <div className="sh">
        <h3>Findings</h3>
        <span className="sh-n">{active.length}</span>
        {crit > 0 && <span className="chip chip-tool"><span className="dot" />{crit} critical</span>}
        {warn > 0 && <span className="chip chip-quality"><span className="dot" />{warn} warning{warn === 1 ? '' : 's'}</span>}
        <span className="sh-sp" />
        {suppressed.length > 0 && (
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setShowSuppressed((v) => !v)}>
            {showSuppressed ? 'Hide suppressed' : `Show ${suppressed.length} suppressed`}
          </button>
        )}
      </div>

      <div className="fgroups">
        {shown.map((g) => {
          const step = stepOf(g.node)
          const st = STATUS_META[mapStatus(step?.status)]
          const full = groups.find((x) => x.node === g.node) ?? g
          const n = full.rows.reduce((k, r) => k + r.all.length, 0)
          return (
            <div key={g.node} className="fgroup">
              <div className="fgroup-h">
                <button type="button" className="fgroup-n" onClick={() => onSelectNode(g.node)}>{g.node}</button>
                {g.node === culprit
                  ? <span className="chip chip-tool chip-solid">root cause</span>
                  : step && <span className={`chip ${st.chip}`}><span className="dot" />{st.label}</span>}
                <span className="fgroup-c">{n} finding{n === 1 ? '' : 's'}</span>
              </div>
              {g.rows.map((r) => {
                const f = r.lead
                const metas = Array.from(new Map(r.all.map((x) => {
                  const m = findingMeta(x)
                  return [`${m.category}${m.label}`, m] as const
                })).values())
                const lead = metas[0]
                const conf = r.all.map((x) => x.confidence).filter((c): c is number => typeof c === 'number')
                const tip = [
                  Array.from(new Set(r.all.map((x) => x.source))).join(' + '),
                  conf.length ? `confidence ${Math.max(...conf).toFixed(2)}` : '',
                  f.origin_node && f.origin_node !== f.node ? `origin ${f.origin_node}` : '',
                ].filter(Boolean).join(' · ')
                return (
                  <div
                    key={r.key}
                    role="button"
                    tabIndex={0}
                    title={tip}
                    className={`frow${f.suppressed ? ' off' : ''}`}
                    onClick={() => onSelectNode(f.node)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectNode(f.node) } }}
                  >
                    <span className="frow-sev" style={{ background: SEV_COLOR[f.severity] ?? 'var(--ink-4)' }} />
                    <div style={{ minWidth: 0 }}>
                      <div className="frow-chips">
                        <span className={`chip ${lead.chip}`}>{lead.label}</span>
                        {metas.length > 1 && (
                          <span className="sh-n" title={metas.slice(1).map((m) => `${m.category} · ${m.label}`).join('\n')}>+{metas.length - 1}</span>
                        )}
                        {f.field_path && !f.reason.includes(f.field_path) && <code className="frow-field">{f.field_path}</code>}
                      </div>
                      <p className="frow-text clamp"><Prose text={sentence(f)} who={culprit} /></p>
                    </div>
                    {!f.suppressed && <FixPromptButton runId={run.run_id} node={f.origin_node ?? f.node} className="btn btn-sm btn-ghost frow-fix" />}
                  </div>
                )
              })}
            </div>
          )
        })}
        {totalRows > FOLD_AT && (
          <button type="button" className="fmore" onClick={() => setShowAll((v) => !v)}>
            {showAll ? 'Show fewer' : `Show ${totalRows - FOLD_AT} more`}
          </button>
        )}
      </div>
    </section>
  )
}
