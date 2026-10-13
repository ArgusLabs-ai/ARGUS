'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { short, statusClass, when } from '@/lib/format'
import { supabase, type Card } from '@/lib/supabase'

const PAGE = 50
type Filter = 'all' | 'not_clean' | 'clean'

export default function Runs() {
  const [rows, setRows] = useState<Omit<Card, 'card'>[] | null>(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<Filter>('not_clean')
  const [page, setPage] = useState(0)

  useEffect(() => {
    let q = supabase
      .from('tele_cards')
      .select('id, received_at, install, run, fingerprint, status, argus_version, ci, n_steps, n_findings')
      .order('received_at', { ascending: false })
      .range(page * PAGE, page * PAGE + PAGE - 1)
    if (filter === 'clean') q = q.eq('status', 'clean')
    if (filter === 'not_clean') q = q.neq('status', 'clean')
    setRows(null)
    q.then(({ data, error }) => (error ? setError(error.message) : setRows(data as Omit<Card, 'card'>[])))
  }, [filter, page])

  if (error) return <p className="err">Could not load: {error}</p>

  return (
    <main>
      <h1>Runs</h1>
      <p className="lead">One card per graded run, newest first. Names are per-install hashes.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        {(['not_clean', 'all', 'clean'] as Filter[]).map((f) => (
          <button
            key={f}
            className={filter === f ? '' : 'ghost'}
            onClick={() => {
              setFilter(f)
              setPage(0)
            }}
          >
            {f === 'not_clean' ? 'Not clean' : f === 'all' ? 'All' : 'Clean'}
          </button>
        ))}
      </div>
      <div className="panel scroll">
        <table>
          <thead>
            <tr>
              <th>Received</th>
              <th>Verdict</th>
              <th>Version</th>
              <th className="num">Steps</th>
              <th className="num">Findings</th>
              <th>CI</th>
              <th>Install</th>
              <th>Pipeline</th>
            </tr>
          </thead>
          <tbody>
            {!rows && (
              <tr>
                <td colSpan={8} className="muted">
                  Loading…
                </td>
              </tr>
            )}
            {rows?.map((r) => (
              <tr key={r.id}>
                <td>
                  <Link href={`/runs/${r.id}`}>{when(r.received_at)}</Link>
                </td>
                <td>
                  <span className={statusClass(r.status)}>{r.status}</span>
                </td>
                <td className="mono">{r.argus_version}</td>
                <td className="num">{r.n_steps}</td>
                <td className="num">{r.n_findings}</td>
                <td>{r.ci ? 'yes' : ''}</td>
                <td className="mono">{short(r.install)}</td>
                <td className="mono">{short(r.fingerprint)}</td>
              </tr>
            ))}
            {rows?.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  Nothing here.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <button className="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>
          Newer
        </button>
        <button className="ghost" disabled={(rows?.length ?? 0) < PAGE} onClick={() => setPage(page + 1)}>
          Older
        </button>
      </div>
    </main>
  )
}
