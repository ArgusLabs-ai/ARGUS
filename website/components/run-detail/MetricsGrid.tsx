'use client'

import type { RunRecord } from '@/lib/types'
import { formatDur, fmtCost, fmtTokens } from '@/lib/run-utils'

const C_GREEN = 'var(--success)'
const C_AMBER = 'var(--warning)'
const C_RED = 'var(--failure)'
const C_GREEN_HEX = 'var(--ok)'
const C_AMBER_HEX = 'var(--quality)'
const C_RED_HEX = 'var(--tool)'

interface MetricCardProps {
  icon: React.ReactNode
  label: string
  value: string
  color?: string
}

function MetricCard({ icon, label, value, color }: MetricCardProps) {
  return (
    <div
      className="rounded-lg border border-border bg-card px-2 py-2 flex flex-col items-center justify-center text-center min-h-[68px]"
    >
      <div className="mb-1">{icon}</div>
      <div>
        <div className="text-[10.5px] font-medium mb-0.5" style={{ color: 'var(--text-tertiary)' }}>
          {label}
        </div>
        <div
          className="text-[17px] font-bold tabular-nums tracking-[-0.03em] leading-none"
          style={{ color: color ?? 'var(--foreground)' }}
        >
          {value}
        </div>
      </div>
    </div>
  )
}

function MetricIcon({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <div className="w-6 h-6 rounded-md flex items-center justify-center shrink-0" style={{ background: color.startsWith('var(') ? `color-mix(in srgb, ${color} 4%, transparent)` : `${color}0a`, color }}>
      {children}
    </div>
  )
}

export default function MetricsGrid({ run, compact = false }: { run: RunRecord; compact?: boolean }) {
  const steps = run.steps ?? []
  const totalNodes = steps.length
  const passedNodes = steps.filter((s) => s.status === 'pass').length
  const failedNodes = steps.filter((s) => !['pass', 'skipped', 'retried', 'degraded_input'].includes(s.status)).length
  const successRate = totalNodes > 0 ? Math.round((passedNodes / totalNodes) * 100) : null

  const completed = run.completed_at != null

  const severityOrder = ['critical', 'warning', 'info', 'ok']
  const worstSeverity = steps.reduce((worst, s) => {
    const sev = s.inspection?.severity
    if (!sev) return worst
    return severityOrder.indexOf(sev) < severityOrder.indexOf(worst) ? sev : worst
  }, 'ok' as string)

  const severityColor: Record<string, string> = {
    critical: C_RED,
    warning: C_AMBER,
    info: 'var(--primary)',
    ok: C_GREEN,
  }
  const statusSeverity =
    run.overall_status === 'clean'
      ? 'ok'
      : run.overall_status === 'interrupted'
        ? 'warning'
        : 'critical'
  const severityDisplay = statusSeverity === 'ok' ? 'OK' : statusSeverity.charAt(0).toUpperCase() + statusSeverity.slice(1)
  const severityDisplayColor = severityColor[statusSeverity] ?? 'var(--ink-4)'

  const totalLLMCalls = run.total_llm_calls ?? 0
  const totalTokens = run.total_tokens ?? 0
  const totalCost = run.total_cost_usd ?? null
  const hasLLMData = totalLLMCalls > 0 || totalTokens > 0

  const nodeCosts = steps
    .filter((s) => s.llm_usage?.total_cost_usd != null && s.llm_usage.total_cost_usd > 0)
    .map((s) => ({
      name: s.node_name,
      cost: s.llm_usage!.total_cost_usd!,
      tokens: s.llm_usage!.total_tokens,
      calls: s.llm_usage!.calls.length,
    }))
    .sort((a, b) => b.cost - a.cost)

  if (compact) {
    return (
      <div className="card rounded-xl p-3.5">
        <div className="flex items-center gap-1.5 mb-2">
          <h3 className="text-[13px] font-bold tracking-[-0.01em]" style={{ color: 'var(--foreground)' }}>Metrics</h3>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <MetricCard
            icon={<MetricIcon color="var(--primary)"><svg width="13" height="13" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.35"/><path d="M7 4v3.5l2 1.5" stroke="currentColor" strokeWidth="1.35" strokeLinecap="round"/></svg></MetricIcon>}
            label="Duration"
            value={formatDur(run.duration_ms)}
            color="var(--foreground)"
          />
          <MetricCard
            icon={<MetricIcon color={C_AMBER}><svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M2 10l3-3 2.5 1.5L12 4" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round"/></svg></MetricIcon>}
            label="Success Rate"
            value={successRate !== null ? `${successRate}%` : '\u2014'}
            color={successRate === null ? 'var(--ink-4)' : C_AMBER}
          />
          <MetricCard
            icon={<MetricIcon color={C_RED}><svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M7 3.5v4M7 10h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg></MetricIcon>}
            label="Failures"
            value={`${failedNodes}`}
            color={C_RED}
          />
          <MetricCard
            icon={<MetricIcon color={severityDisplayColor}><svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M7 2l5.5 9H1.5L7 2Z" stroke="currentColor" strokeWidth="1.45" fill="none" strokeLinejoin="round"/></svg></MetricIcon>}
            label="Severity"
            value={severityDisplay}
            color={severityDisplayColor}
          />
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[12px] uppercase tracking-widest font-semibold" style={{ color: 'var(--text-tertiary)' }}>
          Metrics
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          icon={<MetricIcon color="var(--primary)"><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="var(--iris)" strokeWidth="1.2"/><path d="M7 4v3.5l2 1.5" stroke="var(--iris)" strokeWidth="1.2" strokeLinecap="round"/></svg></MetricIcon>}
          label="Duration"
          value={formatDur(run.duration_ms)}
        />
        <MetricCard
          icon={<MetricIcon color={successRate === 100 ? C_GREEN : C_AMBER}><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 10l3-3 2.5 1.5L12 4" stroke={successRate === 100 ? C_GREEN : C_AMBER} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg></MetricIcon>}
          label="Success Rate"
          value={successRate !== null ? `${successRate}%` : '\u2014'}
          color={successRate === 100 ? C_GREEN : successRate === null ? 'var(--ink-4)' : C_AMBER}
        />
        <MetricCard
          icon={<MetricIcon color={failedNodes > 0 ? C_RED : C_GREEN}><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 4v3M7 9h.01" stroke={failedNodes > 0 ? C_RED : C_GREEN} strokeWidth="1.3" strokeLinecap="round"/></svg></MetricIcon>}
          label="Failures"
          value={`${failedNodes}`}
          color={failedNodes > 0 ? C_RED : C_GREEN}
        />
        <MetricCard
          icon={<MetricIcon color={severityColor[worstSeverity] ?? 'var(--ink-4)'}><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2l5.5 9H1.5L7 2Z" stroke={severityColor[worstSeverity] ?? 'var(--ink-4)'} strokeWidth="1.2" fill="none"/></svg></MetricIcon>}
          label="Severity"
          value={worstSeverity}
          color={severityColor[worstSeverity] ?? 'var(--ink-4)'}
        />
        {hasLLMData && (
          <>
            <MetricCard
              icon={<MetricIcon color="var(--primary)"><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="2" y="3" width="10" height="8" rx="1.5" stroke="var(--iris)" strokeWidth="1.2"/><path d="M5 7h4M5 9h2" stroke="var(--iris)" strokeWidth="1" strokeLinecap="round"/></svg></MetricIcon>}
              label="LLM Calls"
              value={`${totalLLMCalls}`}
            />
            <MetricCard
              icon={<MetricIcon color="var(--coherence)"><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 5h8M3 7h5M3 9h3" stroke="var(--coherence)" strokeWidth="1.2" strokeLinecap="round"/></svg></MetricIcon>}
              label="Tokens"
              value={fmtTokens(totalTokens)}
            />
            {totalCost !== null && (
              <MetricCard
                icon={<MetricIcon color={C_GREEN}><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke={C_GREEN} strokeWidth="1.2"/><path d="M7 4.5v5M5.5 6h3a1 1 0 010 2h-3" stroke={C_GREEN} strokeWidth="1" strokeLinecap="round"/></svg></MetricIcon>}
                label="Cost"
                value={fmtCost(totalCost)}
                color={C_GREEN}
              />
            )}
            <MetricCard
              icon={<MetricIcon color={completed ? C_GREEN : C_AMBER}><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M4 7l2.5 2.5L10 5" stroke={completed ? C_GREEN : C_AMBER} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/></svg></MetricIcon>}
              label="Completed"
              value={completed ? 'Yes' : 'No'}
              color={completed ? C_GREEN : C_AMBER}
            />
          </>
        )}
        {!hasLLMData && (
          <MetricCard
            icon={<MetricIcon color={completed ? C_GREEN : C_AMBER}><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M4 7l2.5 2.5L10 5" stroke={completed ? C_GREEN : C_AMBER} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/></svg></MetricIcon>}
            label="Completed"
            value={completed ? 'Yes' : 'No'}
            color={completed ? C_GREEN : C_AMBER}
          />
        )}
      </div>

      {/* Failure breakdown */}
      {failedNodes > 0 && (
        <div className="card rounded-xl mt-3 px-4 py-2.5 text-[12px]">
          <div className="flex items-center gap-4 flex-wrap text-muted-foreground">
            {steps.filter((s) => s.inspection?.has_tool_failure).length > 0 && (
              <span>tool: <span className="font-mono font-semibold" style={{ color: C_AMBER }}>{steps.filter((s) => s.inspection?.has_tool_failure).length}</span></span>
            )}
            {steps.filter((s) => s.status === 'fail' && s.inspection?.is_silent_failure).length > 0 && (
              <span>context: <span className="font-mono font-semibold" style={{ color: C_AMBER }}>{steps.filter((s) => s.status === 'fail' && s.inspection?.is_silent_failure).length}</span></span>
            )}
            {steps.filter((s) => s.status === 'semantic_fail').length > 0 && (
              <span>semantic: <span className="font-mono font-semibold" style={{ color: 'var(--semantic)' }}>{steps.filter((s) => s.status === 'semantic_fail').length}</span></span>
            )}
            {steps.filter((s) => s.status === 'crashed').length > 0 && (
              <span>crash: <span className="font-mono font-semibold" style={{ color: C_RED }}>{steps.filter((s) => s.status === 'crashed').length}</span></span>
            )}
            {run.first_failure_step && (
              <>
                <span style={{ color: 'var(--line-2)' }}>&middot;</span>
                <span>first failure: <span className="font-mono font-semibold" style={{ color: C_RED }}>{run.first_failure_step}</span></span>
              </>
            )}
          </div>
        </div>
      )}

      {/* Per-node cost */}
      {nodeCosts.length >= 2 && (
        <div className="mt-3 card rounded-xl overflow-hidden">
          <div className="px-4 py-2.5" style={{ background: 'var(--card)' }}>
            <span className="text-[11px] uppercase tracking-widest font-semibold" style={{ color: 'var(--text-tertiary)' }}>Per-Node Cost</span>
          </div>
          <div className="px-4 py-2">
            {nodeCosts.map((nc, i) => (
              <div
                key={i}
                className="flex items-baseline gap-0 py-2 text-[12px]"
                style={{ borderBottom: i < nodeCosts.length - 1 ? '1px solid var(--border)' : 'none' }}
              >
                <span className="font-mono w-[140px] truncate shrink-0 text-muted-foreground">{nc.name}</span>
                <span className="font-mono w-[60px] text-right shrink-0" style={{ color: C_GREEN }}>{fmtCost(nc.cost)}</span>
                <span className="font-mono ml-4 text-muted-foreground">
                  {fmtTokens(nc.tokens)} tok &middot; {nc.calls} call{nc.calls !== 1 ? 's' : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
