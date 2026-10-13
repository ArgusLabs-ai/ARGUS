'use client'

import { useEffect, useState } from 'react'
import DailyChart, { type Day } from '@/components/DailyChart'
import { pct, statusClass } from '@/lib/format'
import { supabase } from '@/lib/supabase'

type DailyRow = { day: string; cards: number; installs: number; not_clean: number }
type StatusRow = { status: string; argus_version: string; cards: number; installs: number }

const DAYS = 30

function lastDays(rows: DailyRow[]): Day[] {
  const byDay = new Map(rows.map((r) => [r.day, r.cards]))
  const out: Day[] = []
  for (let i = DAYS - 1; i >= 0; i--) {
    const day = new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10)
    out.push({ day, cards: byDay.get(day) ?? 0 })
  }
  return out
}

export default function Overview() {
  const [daily, setDaily] = useState<DailyRow[] | null>(null)
  const [status, setStatus] = useState<StatusRow[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    const since = new Date(Date.now() - DAYS * 86_400_000).toISOString().slice(0, 10)
    Promise.all([
      supabase.from('tele_daily').select('*').gte('day', since).order('day'),
      supabase.from('tele_status').select('*').order('cards', { ascending: false }),
    ]).then(([d, s]) => {
      if (d.error || s.error) return setError((d.error ?? s.error)!.message)
      setDaily(d.data as DailyRow[])
      setStatus(s.data as StatusRow[])
    })
  }, [])

  if (error) return <p className="err">Could not load: {error}</p>
  if (!daily) return <p className="muted">Loading…</p>

  const cards = daily.reduce((n, r) => n + r.cards, 0)
  const notClean = daily.reduce((n, r) => n + r.not_clean, 0)
  const today = daily.find((r) => r.day === new Date().toISOString().slice(0, 10))
  const versions = new Set(status.map((s) => s.argus_version)).size

  return (
    <main>
      <h1>Overview</h1>
      <p className="lead">Shape cards from Argus installs, last {DAYS} days (UTC).</p>

      <div className="tiles">
        <Tile k={`Cards, ${DAYS} days`} v={cards} />
        <Tile k="Not clean" v={`${pct(notClean, cards)}`} />
        <Tile k="Installs today" v={today?.installs ?? 0} />
        <Tile k="Argus versions seen" v={versions} />
      </div>

      <h2>Cards per day</h2>
      <div className="panel">
        {cards === 0 ? <p className="muted">No cards yet.</p> : <DailyChart days={lastDays(daily)} />}
      </div>

      <h2>Verdicts by version (all time)</h2>
      <div className="panel scroll">
        <table>
          <thead>
            <tr>
              <th>Verdict</th>
              <th>Version</th>
              <th className="num">Cards</th>
              <th className="num">Installs</th>
            </tr>
          </thead>
          <tbody>
            {status.map((s) => (
              <tr key={`${s.status}-${s.argus_version}`}>
                <td>
                  <span className={statusClass(s.status)}>{s.status}</span>
                </td>
                <td className="mono">{s.argus_version}</td>
                <td className="num">{s.cards}</td>
                <td className="num">{s.installs}</td>
              </tr>
            ))}
            {status.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No cards yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </main>
  )
}

function Tile({ k, v }: { k: string; v: number | string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  )
}
