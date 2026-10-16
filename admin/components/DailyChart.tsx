'use client'

import { useState } from 'react'

export type Day = { day: string; cards: number }

const W = 760
const H = 160
const PAD = { top: 12, right: 8, bottom: 22, left: 8 }
const GAP = 2 // surface gap between adjacent bars

// One series, so one accent hue, no legend (the heading names it).
// Bars: 4px rounded data-end, square at the baseline; hover tooltip per bar
// with the whole column as the hit target; a table view below for exact values.
export default function DailyChart({ days }: { days: Day[] }) {
  const [hover, setHover] = useState<number | null>(null)
  const max = Math.max(1, ...days.map((d) => d.cards))
  const plotW = W - PAD.left - PAD.right
  const plotH = H - PAD.top - PAD.bottom
  const col = plotW / Math.max(days.length, 1)
  const barW = Math.max(col - GAP, 1)

  const bar = (i: number, v: number) => {
    const h = (v / max) * plotH
    const x = PAD.left + i * col
    const y = PAD.top + plotH - h
    const r = Math.min(4, barW / 2, h)
    return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + barW - r} Q${x + barW},${y} ${x + barW},${y + r} V${y + h} Z`
  }

  const tipDay = hover === null ? null : days[hover]
  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Cards received per day">
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={PAD.top + plotH}
          y2={PAD.top + plotH}
          stroke="var(--line-2)"
        />
        {days.map((d, i) => (
          <g key={d.day} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
            <rect x={PAD.left + i * col} y={PAD.top} width={col} height={plotH} fill="transparent" />
            {d.cards > 0 && (
              <path d={bar(i, d.cards)} fill="var(--accent)" opacity={hover === null || hover === i ? 1 : 0.45} />
            )}
          </g>
        ))}
        <text x={PAD.left} y={H - 6} fontSize="11" fill="var(--ink-3)">
          {days[0]?.day}
        </text>
        <text x={W - PAD.right} y={H - 6} fontSize="11" fill="var(--ink-3)" textAnchor="end">
          {days[days.length - 1]?.day}
        </text>
        <text x={W - PAD.right} y={PAD.top - 2} fontSize="11" fill="var(--ink-3)" textAnchor="end">
          max {max}
        </text>
      </svg>
      {tipDay && hover !== null && (
        <div
          className="chart-tip"
          style={{
            left: `${((PAD.left + hover * col + col / 2) / W) * 100}%`,
            top: `${((PAD.top + plotH - (tipDay.cards / max) * plotH) / H) * 100}%`,
          }}
        >
          <strong>{tipDay.cards}</strong> cards · {tipDay.day}
        </div>
      )}
      <details>
        <summary className="muted">Table view</summary>
        <table>
          <thead>
            <tr>
              <th>Day</th>
              <th className="num">Cards</th>
            </tr>
          </thead>
          <tbody>
            {days.map((d) => (
              <tr key={d.day}>
                <td className="mono">{d.day}</td>
                <td className="num">{d.cards}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  )
}
