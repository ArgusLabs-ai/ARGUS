'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useEffect, useState } from 'react'
import ShapeTree, { shapeLabel } from '@/components/ShapeTree'
import { short, statusClass, when } from '@/lib/format'
import { supabase, type Card } from '@/lib/supabase'

export default function RunDetail() {
  const { id } = useParams<{ id: string }>()
  const [card, setCard] = useState<Card | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    supabase
      .from('tele_cards')
      .select('*')
      .eq('id', id)
      .maybeSingle()
      .then(({ data, error }) => {
        if (error) return setError(error.message)
        if (!data) return setError('No such run, or you cannot see it.')
        setCard(data as Card)
      })
  }, [id])

  if (error) return <p className="err">{error}</p>
  if (!card) return <p className="muted">Loading…</p>

  const body = card.card
  const blamed = new Set(body.blamed)
  // Hashes are unreadable; number nodes in the order they ran instead.
  const label = new Map<string, string>()
  body.steps.forEach((s) => label.has(s.node) || label.set(s.node, `node ${label.size + 1}`))
  const name = (h: string) => label.get(h) ?? short(h)

  return (
    <main>
      <p>
        <Link href="/runs">← Runs</Link>
      </p>
      <h1>
        <span className={statusClass(card.status)}>{card.status}</span>{' '}
        <span className="mono">{card.run}</span>
      </h1>
      <p className="lead">
        {when(card.received_at)} · Argus {card.argus_version} · LangGraph {card.langgraph_version ?? '—'} · Python{' '}
        {card.python} {card.ci && '· CI'} · {body.graph.nodes} nodes, {body.graph.edges} edges
        {body.graph.cyclic && ', cyclic'} · install <span className="mono">{short(card.install)}</span>
      </p>

      <h2>Findings</h2>
      <div className="panel scroll">
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th>Kind</th>
              <th>Severity</th>
              <th>Source</th>
              <th>On</th>
              <th>Origin</th>
              <th>Ignored</th>
            </tr>
          </thead>
          <tbody>
            {body.findings.map((f, i) => (
              <tr key={i}>
                <td className="mono">{f.type}</td>
                <td>{f.heuristic ? <span className="chip warn">heuristic</span> : 'strict'}</td>
                <td>
                  <span className={f.severity === 'critical' ? 'chip bad' : 'chip'}>{f.severity}</span>
                </td>
                <td>{f.source}</td>
                <td>{name(f.node)}</td>
                <td>{f.origin ? name(f.origin) : ''}</td>
                <td>{f.suppressed ? 'yes' : ''}</td>
              </tr>
            ))}
            {body.findings.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  Nothing fired.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <h2>Steps</h2>
      {body.steps.map((s, i) => (
        <div key={i} className={`step ${blamed.has(s.node) ? 'blamed' : ''}`}>
          <div className="row">
            <strong>{name(s.node)}</strong>
            <span className={statusClass(s.status)}>{s.status}</span>
            {blamed.has(s.node) && <span className="chip bad">blamed</span>}
            {s.error && <span className="chip bad">raised</span>}
            {s.attempt > 0 && <span className="chip">attempt {s.attempt + 1}</span>}
            <span className="muted">{s.ms ?? 0} ms</span>
            {s.review.map((r, j) => (
              <span key={j} className="chip accent">
                reviewer {r.role}: {r.kind}
              </span>
            ))}
          </div>
          <div style={{ marginTop: 8 }}>
            <div className="muted">Update</div>
            {s.update ? <ShapeTree shape={s.update} /> : <p className="muted">none (routing only)</p>}
          </div>
          {s.tools.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div className="muted">Tools</div>
              <ul className="tree">
                {s.tools.map((t, j) => (
                  <li key={j}>
                    <span className="mono">{short(t.name)}</span> {t.error && <span className="chip bad">error</span>}{' '}
                    {t.output && <span>{shapeLabel(t.output)}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ))}

      <details style={{ marginTop: 16 }}>
        <summary className="muted">Raw card</summary>
        <pre className="mono panel scroll">{JSON.stringify(body, null, 2)}</pre>
      </details>
    </main>
  )
}
