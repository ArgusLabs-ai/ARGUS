'use client'

/* Filter chips for the run list. Each chip is a `key: value` pair; clicking
   removes it. "+ Filter" opens a small picker of observed values. */

import { useEffect, useRef, useState } from 'react'
import type { RunSummary } from '@/lib/types'
import {
  FILTER_KEYS,
  uniqueFilterValues,
  filterValueLabel,
  type FilterKey,
  type RunFilter,
} from '@/lib/run-filters'
import { cn } from '@/lib/utils'

export default function RunFilterBar({
  runs,
  filters,
  allFilters,
  onChange,
}: {
  runs: RunSummary[]
  /** Chips to display (a subset of allFilters). */
  filters: RunFilter[]
  /** The complete filter set the URL carries; edits are applied to this. */
  allFilters?: RunFilter[]
  onChange: (next: RunFilter[]) => void
}) {
  const base = allFilters ?? filters
  const [adding, setAdding] = useState<FilterKey | null>(null)
  const popRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!adding) return
    const onDoc = (e: MouseEvent) => {
      if (popRef.current && !popRef.current.contains(e.target as Node)) setAdding(null)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [adding])

  const remove = (f: RunFilter) => onChange(base.filter((x) => !(x.key === f.key && x.value === f.value)))
  const add = (key: FilterKey, value: string) => {
    if (!base.some((f) => f.key === key && f.value === value)) onChange([...base, { key, value }])
    setAdding(null)
  }

  const values = adding ? uniqueFilterValues(runs, adding) : []

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      {filters.map((f, i) => (
        <button
          key={`${f.key}:${f.value}:${i}`}
          type="button"
          onClick={() => remove(f)}
          className="chip chip-idle"
          style={{ cursor: 'pointer', fontFamily: 'var(--mono)', fontSize: 11.5, gap: 5 }}
          title="Remove filter"
        >
          <span style={{ color: 'var(--ink-4)' }}>{f.key}:</span>
          {filterValueLabel(runs, f)}
          <span aria-hidden style={{ color: 'var(--ink-4)', marginLeft: 2 }}>×</span>
        </button>
      ))}
      <div style={{ position: 'relative' }} ref={popRef}>
        <button
          type="button"
          className="fpill"
          onClick={() => setAdding((v) => (v ? null : 'node'))}
          aria-expanded={!!adding}
        >
          + Filter
        </button>
        {adding && (
          <div
            className="modal"
            style={{ position: 'absolute', left: 0, top: 'calc(100% + 4px)', zIndex: 30, width: 260, maxWidth: 'none' }}
          >
            <div className="seg" style={{ margin: 8, display: 'flex' }}>
              {FILTER_KEYS.map((k) => (
                <button key={k.key} type="button" aria-selected={adding === k.key} onClick={() => setAdding(k.key)} style={{ flex: 1 }}>
                  {k.label}
                </button>
              ))}
            </div>
            <ul style={{ margin: 0, padding: '0 0 6px', listStyle: 'none', maxHeight: 220, overflowY: 'auto' }}>
              {values.length === 0 ? (
                <li className="ex-empty" style={{ paddingLeft: 12 }}>No values yet</li>
              ) : (
                values.map((value) => (
                  <li key={value}>
                    <button
                      type="button"
                      onClick={() => add(adding, value)}
                      className={cn('ex-item')}
                      style={{ paddingLeft: 12 }}
                    >
                      <span className="lb">{filterValueLabel(runs, { key: adding, value })}</span>
                    </button>
                  </li>
                ))
              )}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}
