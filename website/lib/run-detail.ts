/* Shared derivations for the run detail workspace: the headline verdict,
   the status → tone mapping for step rows, and one-sentence step notes. */

import type { Finding, NodeEvent, RunRecord, StepStatus } from './types'
import type { StatusTone } from './workspace'

const SEV_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 }

/** Active findings, most severe first, stable within a severity. */
export function activeFindings(run: RunRecord): Finding[] {
  return (run.findings ?? [])
    .filter((f) => !f.suppressed)
    .slice()
    .sort((a, b) => (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3))
}

/** The culprit node: where the failure originated. */
export function culpritNode(run: RunRecord): string | null {
  return run.root_cause_chain?.[0] ?? run.first_failure_step ?? null
}

/** The one finding the overview leads with. Prefers the culprit's most
    severe finding, then the most severe finding anywhere. */
export function headlineFinding(run: RunRecord): Finding | null {
  const active = activeFindings(run)
  if (!active.length) return null
  const who = culpritNode(run)
  if (who) {
    const own = active.find((f) => (f.origin_node ?? f.node) === who)
    if (own) return own
  }
  return active[0]
}

/** Ordered failure chain from origin to where it surfaced. */
export function failureChain(run: RunRecord): string[] {
  const chain = [...(run.root_cause_chain ?? [])]
  const crash = (run.steps ?? []).find((s) => s.status === 'crashed')
  if (crash && !chain.includes(crash.node_name)) chain.push(crash.node_name)
  if (!chain.length && run.first_failure_step) chain.push(run.first_failure_step)
  return chain
}

export function stepTone(status: StepStatus | string | undefined): StatusTone {
  switch (status) {
    case 'pass': return 'mute'
    case 'crashed': case 'fail': return 'bad'
    case 'semantic_fail': return 'sem'
    case 'degraded_input': case 'interrupted': case 'retried': return 'warn'
    default: return 'mute'
  }
}

export function stepWord(status: StepStatus | string | undefined): string {
  switch (status) {
    case 'pass': return 'pass'
    case 'crashed': return 'crashed'
    case 'fail': return 'fail'
    case 'semantic_fail': return 'semantic_fail'
    case 'degraded_input': return 'degraded'
    case 'interrupted': return 'interrupted'
    case 'retried': return 'retried'
    case 'skipped': return 'not reached'
    default: return status ?? '—'
  }
}

/** Failure flag class for a step row: red rule, purple rule, or none. */
export function stepFlag(status: StepStatus | string | undefined): '' | 'flag' | 'flag-sem' | 'flag-warn' {
  switch (status) {
    case 'crashed': case 'fail': return 'flag'
    case 'semantic_fail': return 'flag-sem'
    case 'degraded_input': case 'interrupted': return 'flag-warn'
    default: return ''
  }
}

/** Strips the leading "Node `x` ..." boilerplate so a reason reads as a
    clause after the node name has already been shown. */
export function trimReason(reason: string, node: string): string {
  let r = reason.trim()
  const lead = new RegExp(`^Node \`${escapeRe(node)}\`\\s*`, 'i')
  r = r.replace(lead, '')
  const judge = new RegExp(`^LLM judge failed \`${escapeRe(node)}\`:\\s*`, 'i')
  r = r.replace(judge, 'failed the LLM judge: ')
  return r
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Short note for a step row: its worst finding, else the exception, else nothing. */
export function stepNote(step: NodeEvent, run: RunRecord): string | null {
  /* Passing rows stay quiet unless something critical landed on them, so
     the eye finds the failures by scanning one column. */
  const own = activeFindings(run).filter((f) => f.node === step.node_name && (step.status !== 'pass' || f.severity === 'critical'))
  if (own.length) return own[0].reason
  if (step.exception) {
    const last = step.exception.split('\n').filter((l) => l.trim()).pop()
    return last ? last.trim() : null
  }
  if (step.status === 'degraded_input' && step.inspection) {
    const up = step.inspection.degraded_upstream_node ?? 'an upstream node'
    const fields = step.inspection.degraded_fields ?? []
    return fields.length
      ? `Received degraded input: \`${fields.join('`, `')}\` missing because \`${up}\` failed to produce it.`
      : `Received degraded input from \`${up}\`.`
  }
  return null
}

/** Number of tool/LLM calls a run made, best-effort. */
export function totalCalls(run: RunRecord): number {
  if (typeof run.total_llm_calls === 'number') return run.total_llm_calls
  return (run.steps ?? []).reduce((n, s) => n + (s.llm_usage?.calls?.length ?? 0), 0)
}

export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return '—'
  return n.toLocaleString()
}

export function fmtCost(usd: number | null | undefined): string {
  if (usd == null) return '—'
  if (usd === 0) return '$0'
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(3)}`
}

export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toTimeString().slice(0, 8)
}
