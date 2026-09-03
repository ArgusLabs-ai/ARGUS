'use client'

import { cn } from '@/lib/utils'
import type { RunStatus, StepStatus, Severity } from '@/lib/types'

/* ── Run Status Badge ──────────────────────────────────────────── */

const RUN_STATUS_CONFIG: Record<
  RunStatus,
  { label: string; color: string }
> = {
  clean:          { label: 'Clean',          color: 'var(--ok)' },
  silent_failure: { label: 'Silent Failure', color: 'var(--quality)' },
  crashed:        { label: 'Crashed',        color: 'var(--tool)' },
  semantic_fail:  { label: 'Semantic Fail',  color: 'var(--semantic)' },
  interrupted:    { label: 'Interrupted',    color: 'var(--ink-3)' },
}

export function RunStatusBadge({
  status,
  size = 'default',
  className,
}: {
  status: RunStatus
  size?: 'default' | 'sm'
  className?: string
}) {
  const c = RUN_STATUS_CONFIG[status] ?? { label: status, color: 'var(--ink-3)' }

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-[4px] border font-medium',
        size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs',
        className,
      )}
      style={{
        color: c.color,
        backgroundColor: `color-mix(in srgb, ${c.color} 10%, transparent)`,
        borderColor: `color-mix(in srgb, ${c.color} 25%, transparent)`,
      }}
    >
      <span className="relative flex h-2 w-2">
        <span
          className="relative inline-flex h-2 w-2 rounded-full"
          style={{ background: c.color }}
        />
      </span>
      {c.label}
    </span>
  )
}

/* ── Step Status Badge ─────────────────────────────────────────── */

const STEP_STATUS_CONFIG: Record<StepStatus, { label: string; color: string }> = {
  pass:           { label: 'pass',           color: 'var(--ok)' },
  degraded_input: { label: 'degraded input', color: 'var(--quality)' },
  fail:           { label: 'fail',           color: 'var(--quality)' },
  crashed:        { label: 'crashed',        color: 'var(--tool)' },
  semantic_fail:  { label: 'semantic fail',  color: 'var(--semantic)' },
  interrupted:    { label: 'interrupted',    color: 'var(--ink-3)' },
  retried:        { label: 'retried',        color: 'var(--ink-3)' },
  skipped:        { label: 'skipped',        color: 'var(--ink-3)' },
}

export function StepStatusBadge({ status, className }: { status: StepStatus; className?: string }) {
  const c = STEP_STATUS_CONFIG[status] ?? { label: status, color: 'var(--ink-3)' }

  return (
    <span
      className={cn('inline-flex items-center gap-1.5 text-[12px] font-medium', className)}
      style={{ color: c.color }}
    >
      <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: c.color }} />
      <span>{c.label}</span>
    </span>
  )
}

/* ── Severity Badge ────────────────────────────────────────────── */

const SEVERITY_COLOR: Record<Severity, string> = {
  critical: 'var(--tool)',
  warning:  'var(--quality)',
  info:     'var(--iris)',
  ok:       'var(--ok)',
}

export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  const color = SEVERITY_COLOR[severity] ?? 'var(--ink-3)'

  return (
    <span
      className={cn('inline-flex items-center px-2 py-0.5 rounded-[4px] border text-[11px] font-semibold', className)}
      style={{
        color,
        backgroundColor: `color-mix(in srgb, ${color} 10%, transparent)`,
        borderColor: `color-mix(in srgb, ${color} 25%, transparent)`,
      }}
    >
      {severity}
    </span>
  )
}
