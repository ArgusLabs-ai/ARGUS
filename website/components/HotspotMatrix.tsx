'use client'

/* Failure hotspots — origin (row) × surfacing node (column). Single hue,
   amber, intensity by count. Sits flush under a quiet caption; no card. */

import { useEffect, useMemo, useState } from 'react'
import { aggregateHotspots, type HotspotCell, type HotspotMatrixData } from '@/lib/hotspots'
import type { RunRecord, RunSummary } from '@/lib/types'

function cellLookup(cells: HotspotCell[]): Map<string, HotspotCell> {
  const map = new Map<string, HotspotCell>()
  for (const c of cells) map.set(`${c.origin}\0${c.node}`, c)
  return map
}

function amber(count: number, max: number): { bg: string; fg: string } {
  if (count <= 0 || max <= 0) return { bg: 'var(--fill-subtle)', fg: 'var(--ink-4)' }
  const t = Math.min(1, count / max)
  const pct = Math.round((0.14 + t * 0.7) * 100)
  return {
    bg: `color-mix(in srgb, var(--quality) ${pct}%, transparent)`,
    fg: t > 0.5 ? 'var(--on-signal)' : 'var(--quality)',
  }
}

export default function HotspotMatrix({
  runs,
  onSelectCell,
}: {
  runs: RunSummary[]
  onSelectCell: (origin: string, node: string) => void
}) {
  const [data, setData] = useState<HotspotMatrixData | null>(null)
  const [open, setOpen] = useState(true)
  const runKey = runs.map((r) => r.run_id).join(',')

  useEffect(() => {
    let cancelled = false
    const failed = runs.filter((r) => r.overall_status !== 'clean').slice(0, 40)

    const fromDetails = async (): Promise<HotspotMatrixData | null> => {
      if (failed.length === 0) return null
      const records = await Promise.all(
        failed.map((r) =>
          fetch(`/api/runs/${r.run_id}`)
            .then((res) => (res.ok ? res.json() : null))
            .catch(() => null),
        ),
      )
      const loaded = records.filter(Boolean) as RunRecord[]
      const built = aggregateHotspots(loaded)
      return built.cells.length ? built : null
    }

    fetch('/api/hotspots', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then(async (payload: HotspotMatrixData | null) => {
        if (cancelled) return
        if (payload && Array.isArray(payload.cells) && payload.cells.length) {
          setData(payload)
          return
        }
        setData(await fromDetails())
      })
      .catch(async () => {
        if (!cancelled) setData(await fromDetails())
      })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runKey])

  const lookup = useMemo(() => cellLookup(data?.cells ?? []), [data])
  const max = useMemo(() => Math.max(0, ...(data?.cells ?? []).map((c) => c.count)), [data])

  if (!data || data.cells.length === 0) return null

  const nodes = data.nodes.slice(0, 18)
  const origins = data.origins.slice(0, 14)

  return (
    <div>
      <p className="cap">
        <span>
          Failure hotspots · origin ↓ × surfaced at →
          <span style={{ color: 'var(--ink-4)' }}> · {data.run_count.toLocaleString()} runs</span>
        </span>
        <a href="#" onClick={(e) => { e.preventDefault(); setOpen((v) => !v) }}>{open ? 'Hide' : 'Show'}</a>
      </p>
      {open && (
        <div style={{ overflowX: 'auto' }}>
          <div
            className="hm"
            style={{ gridTemplateColumns: `minmax(90px, 150px) repeat(${nodes.length}, 28px)` }}
            role="grid"
            aria-label="Failure hotspots"
          >
            <span />
            {nodes.map((n) => (
              <span key={n} className="hm-col" title={n} style={{ alignSelf: 'end', justifySelf: 'center' }}>{n}</span>
            ))}
            {origins.map((origin) => (
              <div key={origin} style={{ display: 'contents' }} role="row">
                <span className="hm-lbl" title={origin} style={{ alignSelf: 'center', paddingRight: 10 }}>{origin}</span>
                {nodes.map((node) => {
                  const cell = lookup.get(`${origin}\0${node}`)
                  const count = cell?.count ?? 0
                  const { bg, fg } = amber(count, max)
                  return (
                    <button
                      key={node}
                      type="button"
                      className="hm-cell"
                      role="gridcell"
                      title={count ? `${origin} → ${node}: ${count}` : `${origin} → ${node}`}
                      onClick={() => count && onSelectCell(origin, node)}
                      style={{ background: bg, color: fg, cursor: count ? 'pointer' : 'default' }}
                      disabled={!count}
                    >
                      {count || ''}
                    </button>
                  )
                })}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
