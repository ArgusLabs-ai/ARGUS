'use client'

import { cn } from '@/lib/utils'
import type { RunStatus, StepStatus, Severity } from '@/lib/types'

/* Status pills use the spec's .chip primitive (globals.css). Each status maps to
   a signal variant; the leading dot is part of the chip. */

/* ── Run Status ────────────────────────────────────────────────── */

const RUN_STATUS: Record<RunStatus, { label: string; variant: string }> = {
  clean:          { label: 'Clean',          variant: 'chip-ok' },
  silent_failure: { label: 'Silent Failure', variant: 'chip-quality' },
  crashed:        { label: 'Crashed',        variant: 'chip-tool' },
  semantic_fail:  { label: 'Semantic Fail',  variant: 'chip-semantic' },
  interrupted:    { label: 'Interrupted',    variant: 'chip-coherence' },
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
  const c = RUN_STATUS[status] ?? { label: status, variant: 'chip-idle' }
  return (
    <span className={cn('chip', c.variant, className)}>
      <span className="dot" />
      {c.label}
    </span>
  )
}

/* ── Step Status ───────────────────────────────────────────────── */

const STEP_STATUS: Record<StepStatus, { label: string; variant: string }> = {
  pass:           { label: 'pass',           variant: 'chip-ok' },
  fail:           { label: 'fail',           variant: 'chip-quality' },
  degraded_input: { label: 'degraded input', variant: 'chip-quality' },
  crashed:        { label: 'crashed',        variant: 'chip-tool' },
  semantic_fail:  { label: 'semantic fail',  variant: 'chip-semantic' },
  interrupted:    { label: 'interrupted',    variant: 'chip-coherence' },
  retried:        { label: 'retried',        variant: 'chip-iris' },
  skipped:        { label: 'skipped',        variant: 'chip-idle' },
}

export function StepStatusBadge({ status, className }: { status: StepStatus; className?: string }) {
  const c = STEP_STATUS[status] ?? { label: status, variant: 'chip-idle' }
  return (
    <span className={cn('chip', c.variant, className)}>
      <span className="dot" />
      {c.label}
    </span>
  )
}

/* ── Severity ──────────────────────────────────────────────────── */

const SEVERITY: Record<Severity, string> = {
  critical: 'chip-tool',
  warning:  'chip-quality',
  info:     'chip-iris',
  ok:       'chip-ok',
}

export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  return (
    <span className={cn('chip', SEVERITY[severity] ?? 'chip-idle', className)}>
      {severity}
    </span>
  )
}
