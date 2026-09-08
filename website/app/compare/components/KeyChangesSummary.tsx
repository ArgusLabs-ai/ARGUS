'use client'

import type { KeyChange } from '../lib/compare-utils'

const ICONS: Record<string, { icon: string; color: string }> = {
  improved:  { icon: '\u2713', color: 'var(--ok)' },
  degraded:  { icon: '\u2717', color: 'var(--tool)' },
  unchanged: { icon: '\u2014', color: 'var(--ink-3)' },
}

export default function KeyChangesSummary({ changes, compact = false }: { changes: KeyChange[]; compact?: boolean }) {
  return (
    <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
      {changes.map((c) => {
        const vis = ICONS[c.type] ?? ICONS.unchanged
        return (
          <div key={c.nodeName} className={compact ? 'flex items-start gap-2' : 'flex items-start gap-2.5'}>
            <span
              className={`${compact ? 'text-[12px] w-4' : 'text-[14px] w-5'} font-bold shrink-0 text-center mt-0.5`}
              style={{ color: vis.color }}
            >
              {vis.icon}
            </span>
            <div className="min-w-0">
              <span className={`${compact ? 'text-[11.5px]' : 'text-[13px]'} font-semibold`} style={{ color: 'var(--foreground)' }}>
                {c.nodeName}:
              </span>
              <span className={`${compact ? 'text-[11.5px]' : 'text-[13px]'} ml-1`} style={{ color: 'var(--text-secondary)' }}>
                {c.description}
              </span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
