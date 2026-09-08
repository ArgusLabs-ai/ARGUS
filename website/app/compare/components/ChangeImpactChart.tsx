'use client'

import type { ChangeImpact } from '../lib/compare-utils'

export default function ChangeImpactChart({ impact, compact = false }: { impact: ChangeImpact; compact?: boolean }) {
  const structural = impact.structural ?? 0
  const total = impact.positive + impact.negative + impact.unchanged + structural
  if (total === 0) return null

  const radius = compact ? 38 : 70
  const stroke = compact ? 10 : 18
  const circumference = 2 * Math.PI * radius
  const center = radius + stroke / 2 + 4

  const posLen = (impact.positive / 100) * circumference
  const negLen = (impact.negative / 100) * circumference
  const uncLen = (impact.unchanged / 100) * circumference
  const strLen = (structural / 100) * circumference

  const segments = [
    { len: posLen, offset: 0, color: 'var(--ok)' },
    { len: negLen, offset: posLen, color: 'var(--tool)' },
    { len: uncLen, offset: posLen + negLen, color: 'var(--line-3)' },
    { len: strLen, offset: posLen + negLen + uncLen, color: 'var(--iris)' },
  ].filter((s) => s.len > 0)

  const values = [
    { pct: impact.positive, label: 'Positive Impact' },
    { pct: impact.negative, label: 'Negative Impact' },
    { pct: impact.unchanged, label: 'No Change' },
    { pct: structural, label: 'Structural' },
  ]
  const dominant = values.reduce((a, b) => (b.pct > a.pct ? b : a))

  return (
    <div className={`flex items-center ${compact ? 'gap-3' : 'gap-5'}`}>
      {/* Donut */}
      <div className="relative shrink-0" style={{ width: center * 2, height: center * 2 }}>
        <svg width={center * 2} height={center * 2} viewBox={`0 0 ${center * 2} ${center * 2}`}>
          {/* Background ring */}
          <circle
            cx={center} cy={center} r={radius}
            fill="none" stroke="var(--card)" strokeWidth={stroke}
          />
          {/* Segments */}
          {segments.map((seg, i) => (
            <circle
              key={i}
              cx={center} cy={center} r={radius}
              fill="none"
              stroke={seg.color}
              strokeWidth={stroke}
              strokeDasharray={`${seg.len} ${circumference - seg.len}`}
              strokeDashoffset={-seg.offset}
              strokeLinecap="butt"
              transform={`rotate(-90 ${center} ${center})`}
            />
          ))}
        </svg>
        {/* Center text */}
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className={`${compact ? 'text-[14px]' : 'text-[18px]'} font-bold`} style={{ color: 'var(--foreground)' }}>
            {dominant.pct}%
          </span>
          <span className={`${compact ? 'text-[8.5px]' : 'text-[10px]'} font-medium`} style={{ color: 'var(--text-tertiary)' }}>
            {dominant.label}
          </span>
        </div>
      </div>

      {/* Legend */}
      <div className={compact ? 'flex flex-col gap-1.5' : 'flex flex-col gap-2'}>
        {[
          { color: 'var(--ok)', label: 'Positive', pct: impact.positive },
          { color: 'var(--line-3)', label: 'No Change', pct: impact.unchanged },
          { color: 'var(--tool)', label: 'Negative', pct: impact.negative },
          ...(structural > 0 ? [{ color: 'var(--iris)', label: 'Structural', pct: structural }] : []),
        ].map((entry) => (
          <div key={entry.label} className="flex items-center gap-2">
            <span className={`${compact ? 'w-2 h-2' : 'w-3 h-3'} rounded-full shrink-0`} style={{ background: entry.color }} />
            <span className={`${compact ? 'text-[11px]' : 'text-[12px]'} font-medium`} style={{ color: 'var(--text-secondary)' }}>{entry.label}</span>
            <span className={`${compact ? 'text-[11px]' : 'text-[12px]'} font-bold`} style={{ color: 'var(--foreground)' }}>{entry.pct}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}
