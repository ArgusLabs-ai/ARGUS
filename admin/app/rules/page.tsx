'use client'

import { useEffect, useMemo, useState } from 'react'
import { pct, when } from '@/lib/format'
import { supabase } from '@/lib/supabase'

type RuleRow = {
  rule: string
  heuristic: boolean
  fires: number
  runs: number
  installs: number
  ignored: number
  in_ci: number
  critical: number
  last_seen: string
  versions: string[]
}

const SORTS = {
  fires: (a: RuleRow, b: RuleRow) => b.fires - a.fires,
  ignored: (a: RuleRow, b: RuleRow) => b.ignored / b.fires - a.ignored / a.fires,
  installs: (a: RuleRow, b: RuleRow) => b.installs - a.installs,
  recent: (a: RuleRow, b: RuleRow) => b.last_seen.localeCompare(a.last_seen),
}

export default function Rules() {
  const [rows, setRows] = useState<RuleRow[] | null>(null)
  const [error, setError] = useState('')
  const [sort, setSort] = useState<keyof typeof SORTS>('fires')

  useEffect(() => {
    supabase
      .from('tele_rule_stats')
      .select('*')
      .then(({ data, error }) => (error ? setError(error.message) : setRows(data as RuleRow[])))
  }, [])

  const sorted = useMemo(() => [...(rows ?? [])].sort(SORTS[sort]), [rows, sort])

  if (error) return <p className="err">Could not load: {error}</p>
  if (!rows) return <p className="muted">Loading…</p>

  return (
    <main>
      <h1>Rules</h1>
      <p className="lead">
        Every rule that fired, all time. <strong>Ignored</strong> counts hits the user silenced with{' '}
        <code>argus ignore</code>: the strongest false-alarm signal until follow-up events ship.
      </p>
      <div className="row" style={{ marginBottom: 12 }}>
        <label className="muted" htmlFor="sort">
          Sort by
        </label>
        <select id="sort" value={sort} onChange={(e) => setSort(e.target.value as keyof typeof SORTS)}>
          <option value="fires">Times fired</option>
          <option value="ignored">Ignored rate</option>
          <option value="installs">Installs</option>
          <option value="recent">Last seen</option>
        </select>
      </div>
      <div className="panel scroll">
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th>Kind</th>
              <th className="num">Fired</th>
              <th className="num">Runs</th>
              <th className="num">Installs</th>
              <th className="num">Ignored</th>
              <th className="num">Critical</th>
              <th className="num">In CI</th>
              <th>Last seen</th>
              <th>Versions</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.rule}>
                <td className="mono">{r.rule}</td>
                <td>
                  <span className={`chip ${r.heuristic ? 'warn' : ''}`}>{r.heuristic ? 'heuristic' : 'strict'}</span>
                </td>
                <td className="num">{r.fires}</td>
                <td className="num">{r.runs}</td>
                <td className="num">{r.installs}</td>
                <td className="num">
                  {r.ignored} <span className="muted">({pct(r.ignored, r.fires)})</span>
                </td>
                <td className="num">{pct(r.critical, r.fires)}</td>
                <td className="num">{pct(r.in_ci, r.fires)}</td>
                <td>{when(r.last_seen)}</td>
                <td className="mono">{[...r.versions].sort().join(', ')}</td>
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={10} className="muted">
                  No rule has fired yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </main>
  )
}
