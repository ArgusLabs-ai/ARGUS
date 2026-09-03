'use client'

import { useState } from 'react'
import type { Candidate, Signature, SignatureStatsData } from './types'

/* Card and row presentation for the approvals views. Split out of page.tsx,
   which had grown to 1027 lines. */

const SEVERITY_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  critical: { bg: 'var(--tool-dim)', border: 'color-mix(in srgb, var(--tool) 34%, transparent)', text: 'var(--tool)' },
  warning: { bg: 'var(--quality-dim)', border: 'color-mix(in srgb, var(--quality) 34%, transparent)', text: 'var(--quality)' },
}

const STRATEGY_COLORS: Record<string, string> = {
  regex: 'var(--iris)',
  contains_ci: 'var(--ok)',
  exact_ci: 'var(--quality)',
  prefix_ci: 'var(--semantic)',
  repetition: 'var(--tool)',
}

// ── Helpers ──────────────────────────────────────────────────

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 30) return `${days}d ago`
  const months = Math.floor(days / 30)
  return `${months}mo ago`
}

function confidenceColor(c: number): string {
  if (c >= 0.8) return 'var(--ok)'
  if (c >= 0.6) return 'var(--quality)'
  return 'var(--tool)'
}

// ── Badges ───────────────────────────────────────────────────

function SeverityBadge({ severity }: { severity: string }) {
  const c = SEVERITY_COLORS[severity] || SEVERITY_COLORS.warning
  return (
    <span
      className="text-[10.5px] font-semibold px-2 py-0.5 rounded-md uppercase tracking-wide"
      style={{ background: c.bg, border: `1px solid ${c.border}`, color: c.text }}
    >
      {severity}
    </span>
  )
}

function StrategyBadge({ strategy }: { strategy: string }) {
  const color = STRATEGY_COLORS[strategy] || 'var(--ink-3)'
  return (
    <span
      className="text-[10.5px] font-mono font-medium px-2 py-0.5 rounded-md"
      style={{ background: `${color}10`, border: `1px solid ${color}30`, color }}
    >
      {strategy}
    </span>
  )
}

function CategoryBadge({ category }: { category: string }) {
  return (
    <span
      className="text-[10.5px] font-medium px-2 py-0.5 rounded-md"
      style={{
        background: 'var(--iris-dim)',
        border: '1px solid color-mix(in srgb, var(--iris) 34%, transparent)',
        color: 'var(--text-secondary)',
      }}
    >
      {category}
    </span>
  )
}

function SourceBadge({ source }: { source: string }) {
  const isShared = source === 'shared'
  return (
    <span
      className="text-[10px] font-semibold px-2 py-0.5 rounded-md uppercase tracking-wide"
      style={{
        background: isShared ? 'var(--iris-dim)' : 'var(--ok-dim)',
        border: `1px solid ${isShared ? 'color-mix(in srgb, var(--iris) 34%, transparent)' : 'color-mix(in srgb, var(--ok) 34%, transparent)'}`,
        color: isShared ? 'var(--iris)' : 'var(--ok)',
      }}
    >
      {isShared ? 'shared' : 'private'}
    </span>
  )
}

// ── Candidate Card (Pending Tab) ─────────────────────────────

export function CandidateCard({
  candidate,
  onApprovePrivate,
  onApproveShared,
  onReject,
  acting,
}: {
  candidate: Candidate
  onApprovePrivate: (id: string) => void
  onApproveShared: (id: string) => void
  onReject: (id: string) => void
  acting: string | null
}) {
  const [expanded, setExpanded] = useState(false)
  const [confirmReject, setConfirmReject] = useState(false)
  const isActing = acting === candidate.id

  return (
    <div
      className="rounded-xl overflow-hidden transition-all"
      style={{
        background: 'var(--bg-surface)',
        border: `1px solid ${
          candidate.severity === 'critical'
            ? 'color-mix(in srgb, var(--tool) 34%, transparent)'
            : 'var(--border-subtle)'
        }`,
      }}
    >
      {/* Header */}
      <div className="px-5 pt-5 pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2 flex-wrap">
            <SeverityBadge severity={candidate.severity} />
            <StrategyBadge strategy={candidate.match_strategy} />
            <CategoryBadge category={candidate.proposed_category} />
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span
              className="text-[10.5px] font-mono px-2 py-0.5 rounded-md"
              style={{ background: 'var(--bg-elevated)', color: 'var(--text-muted)' }}
            >
              {candidate.times_seen}x seen
            </span>
            <span className="text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
              {timeAgo(candidate.last_seen)}
            </span>
          </div>
        </div>

        {/* Pattern */}
        <div
          className="mt-3 rounded-lg px-4 py-3 font-mono text-[13px] break-all"
          style={{
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border-default)',
            color: 'var(--text-primary)',
          }}
        >
          {candidate.pattern}
        </div>

        {/* Description */}
        <p className="mt-3 text-[13px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
          {candidate.description}
        </p>

        {/* Confidence */}
        <div className="mt-3 flex items-center gap-3">
          <span className="text-[11px] font-medium shrink-0" style={{ color: 'var(--text-muted)', minWidth: 72 }}>
            Confidence
          </span>
          <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--bg-elevated)' }}>
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: `${Math.round(candidate.confidence * 100)}%`,
                background: confidenceColor(candidate.confidence),
              }}
            />
          </div>
          <span
            className="text-[11px] font-mono font-semibold shrink-0"
            style={{ color: confidenceColor(candidate.confidence) }}
          >
            {Math.round(candidate.confidence * 100)}%
          </span>
        </div>
      </div>

      {/* Expandable details */}
      {expanded && (
        <div className="px-5 py-4 flex flex-col gap-3" style={{ borderTop: '1px solid var(--border-subtle)' }}>
          {candidate.reasoning && (
            <div>
              <span className="text-[10px] font-semibold uppercase tracking-[0.1em] block mb-1.5" style={{ color: 'var(--text-muted)' }}>
                LLM Reasoning
              </span>
              <p className="text-[12.5px] leading-relaxed italic" style={{ color: 'var(--text-secondary)' }}>
                {candidate.reasoning}
              </p>
            </div>
          )}
          {candidate.evidence.length > 0 && (
            <div>
              <span className="text-[10px] font-semibold uppercase tracking-[0.1em] block mb-1.5" style={{ color: 'var(--text-muted)' }}>
                Evidence
              </span>
              <div className="flex flex-col gap-1">
                {candidate.evidence.map((e, i) => (
                  <div
                    key={i}
                    className="rounded-md px-3 py-2 font-mono text-[11.5px]"
                    style={{ background: 'var(--bg-elevated)', color: 'var(--text-secondary)', wordBreak: 'break-word' }}
                  >
                    {e}
                  </div>
                ))}
              </div>
            </div>
          )}
          {candidate.source_run_ids.length > 0 && (
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-[10px] font-semibold uppercase tracking-[0.1em]" style={{ color: 'var(--text-muted)' }}>
                Runs:
              </span>
              {candidate.source_run_ids.map((rid) => (
                <span
                  key={rid}
                  className="text-[10.5px] font-mono px-2 py-0.5 rounded-md"
                  style={{ background: 'var(--iris-dim)', color: 'var(--iris)' }}
                >
                  {rid.length > 12 ? `${rid.slice(0, 8)}...` : rid}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Actions footer */}
      <div className="px-5 py-3 flex items-center justify-between" style={{ borderTop: '1px solid var(--border-subtle)' }}>
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="text-[12px] font-medium transition-colors"
          style={{ color: 'var(--iris)' }}
        >
          {expanded ? 'Show less' : 'Show details'}
        </button>

        <div className="flex items-center gap-2">
          {confirmReject ? (
            <div className="flex items-center gap-1.5">
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Reject?</span>
              <button
                type="button"
                onClick={() => { onReject(candidate.id); setConfirmReject(false) }}
                disabled={isActing}
                className="px-3 py-1.5 rounded-lg text-[12px] font-semibold"
                style={{ background: 'var(--tool-dim)', color: 'var(--tool)', opacity: isActing ? 0.5 : 1 }}
              >
                Yes
              </button>
              <button
                type="button"
                onClick={() => setConfirmReject(false)}
                className="px-3 py-1.5 rounded-lg text-[12px] font-medium"
                style={{ color: 'var(--text-muted)' }}
              >
                No
              </button>
            </div>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setConfirmReject(true)}
                disabled={isActing}
                className="px-4 py-2 rounded-lg text-[13px] font-medium transition-all"
                style={{ color: 'var(--text-muted)', border: '1px solid var(--border-subtle)', opacity: isActing ? 0.5 : 1 }}
              >
                Reject
              </button>
              <button
                type="button"
                onClick={() => onApprovePrivate(candidate.id)}
                disabled={isActing}
                className="px-4 py-2 rounded-lg text-[13px] font-semibold transition-all"
                style={{
                  background: 'var(--bg-elevated)',
                  color: 'var(--text-secondary)',
                  border: '1px solid var(--border-default)',
                  opacity: isActing ? 0.5 : 1,
                }}
                title="Save to your local detection engine only"
              >
                {isActing ? 'Saving...' : 'Private'}
              </button>
              <button
                type="button"
                onClick={() => onApproveShared(candidate.id)}
                disabled={isActing}
                className="px-4 py-2 rounded-lg text-[13px] font-semibold transition-all flex items-center gap-1.5"
                style={{ background: 'var(--ok)', color: 'var(--on-accent)', opacity: isActing ? 0.5 : 1 }}
                title="Share with all ARGUS users via cloud sync"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M8 2v8M4.5 6.5L8 2l3.5 4.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
                  <path d="M2.5 11v1.5a1 1 0 001 1h9a1 1 0 001-1V11" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
                </svg>
                {isActing ? 'Sharing...' : 'Shared'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Signature Row (Private/Shared Tabs) ──────────────────────

export function SignatureRow({
  sig,
  stats,
  onRemove,
  onToggleDisable,
  acting,
}: {
  sig: Signature
  stats: SignatureStatsData | null
  onRemove: ((id: string) => void) | null
  onToggleDisable: ((id: string, disabled: boolean) => void) | null
  acting: boolean
}) {
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [expanded, setExpanded] = useState(false)

  const isDisabled = sig.metadata.disabled || false
  const hits = stats?.total_hits ?? sig.metadata.total_hits ?? 0
  const runsHit = stats?.runs_hit ?? 0
  const nodesHit = stats?.nodes_hit ?? 0
  const fpCount = stats?.false_positive_count ?? 0
  const lastHit = stats?.last_hit || sig.metadata.last_hit_at || ''
  const hitNodes = stats?.hit_nodes ?? []

  return (
    <div
      className="rounded-xl overflow-hidden transition-all"
      style={{
        background: 'var(--bg-surface)',
        border: `1px solid ${isDisabled ? 'var(--tool-dim)' : 'var(--border-subtle)'}`,
        opacity: isDisabled ? 0.65 : 1,
      }}
    >
      {/* Main content */}
      <div className="px-5 pt-4 pb-3 flex flex-col gap-2.5">
        {/* Header row */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono text-[11px] font-bold shrink-0" style={{ color: 'var(--iris)', minWidth: 48 }}>
            {sig.id}
          </span>
          <SeverityBadge severity={sig.severity} />
          <StrategyBadge strategy={sig.match_strategy} />
          <SourceBadge source={sig.source} />
          {isDisabled && (
            <span
              className="text-[10px] font-semibold px-2 py-0.5 rounded-md uppercase tracking-wide"
              style={{
                background: 'var(--tool-dim)',
                border: '1px solid color-mix(in srgb, var(--tool) 34%, transparent)',
                color: 'var(--tool)',
              }}
            >
              disabled
            </span>
          )}
          {sig.metadata.contributed_by && (
            <span className="text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
              by {sig.metadata.contributed_by}
            </span>
          )}
          <span className="ml-auto text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
            {sig.metadata.approved_at ? timeAgo(sig.metadata.approved_at) : ''}
          </span>
        </div>

        {/* Pattern */}
        <div
          className="rounded-lg px-3.5 py-2.5 font-mono text-[12.5px] break-all"
          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-default)', color: 'var(--text-primary)' }}
        >
          {sig.pattern}
        </div>

        {/* Description */}
        <p className="text-[12.5px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
          {sig.description}
        </p>

        {/* Stats bar */}
        <div
          className="flex items-center gap-4 rounded-lg px-3.5 py-2.5"
          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-default)' }}
        >
          <div className="flex items-center gap-1.5">
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
              <path d="M8 3v5l3.5 2" stroke={hits > 0 ? 'var(--ok)' : 'var(--text-faint)'} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
              <circle cx="8" cy="8" r="6.5" stroke={hits > 0 ? 'var(--ok)' : 'var(--text-faint)'} strokeWidth="1.3"/>
            </svg>
            <span className="text-[12px] font-semibold" style={{ color: hits > 0 ? 'var(--ok)' : 'var(--text-muted)' }}>
              {hits}
            </span>
            <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
              {hits === 1 ? 'catch' : 'catches'}
            </span>
          </div>

          {runsHit > 0 && (
            <div className="flex items-center gap-1">
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>across</span>
              <span className="text-[12px] font-semibold" style={{ color: 'var(--text-secondary)' }}>
                {runsHit}
              </span>
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                {runsHit === 1 ? 'run' : 'runs'}
              </span>
            </div>
          )}

          {fpCount > 0 && (
            <div className="flex items-center gap-1">
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
                <path d="M8 5v3.5M8 11h.01" stroke="var(--quality)" strokeWidth="1.4" strokeLinecap="round"/>
                <path d="M6.86 2.57L1.21 12.28a1.33 1.33 0 001.14 2h11.3a1.33 1.33 0 001.14-2L9.14 2.57a1.33 1.33 0 00-2.28 0z" stroke="var(--quality)" strokeWidth="1.2"/>
              </svg>
              <span className="text-[11px] font-medium" style={{ color: 'var(--quality)' }}>
                {fpCount} disputed
              </span>
            </div>
          )}

          {hits === 0 && !isDisabled && (
            <span className="text-[11px] italic" style={{ color: 'var(--text-faint)' }}>
              No matches yet — waiting for runs
            </span>
          )}

          {lastHit && (
            <span className="ml-auto text-[10.5px]" style={{ color: 'var(--text-faint)' }}>
              last: {timeAgo(lastHit)}
            </span>
          )}
        </div>
      </div>

      {/* Expandable detail + actions */}
      <div
        className="px-5 pb-4 flex items-center justify-between"
      >
        <div className="flex items-center gap-2">
          {hitNodes.length > 0 && (
            <button
              type="button"
              onClick={() => setExpanded(!expanded)}
              className="text-[11px] flex items-center gap-1 transition-all"
              style={{ color: 'var(--text-muted)' }}
            >
              <svg
                width="10" height="10" viewBox="0 0 16 16" fill="none"
                style={{ transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}
              >
                <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              {nodesHit} {nodesHit === 1 ? 'node' : 'nodes'} matched
            </button>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Disable / Enable toggle */}
          {onToggleDisable && (
            <button
              type="button"
              onClick={() => onToggleDisable(sig.id, isDisabled)}
              disabled={acting}
              className="text-[11px] px-2.5 py-1 rounded-md font-medium transition-all"
              style={{
                background: isDisabled ? 'var(--ok-dim)' : 'var(--tool-dim)',
                border: `1px solid ${isDisabled ? 'color-mix(in srgb, var(--ok) 34%, transparent)' : 'var(--tool-dim)'}`,
                color: isDisabled ? 'var(--ok)' : 'var(--tool)',
                opacity: acting ? 0.5 : 1,
              }}
            >
              {isDisabled ? 'Enable' : 'Disable'}
            </button>
          )}

          {/* Remove */}
          {onRemove && (
            <div className="shrink-0">
              {confirmRemove ? (
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => { onRemove(sig.id); setConfirmRemove(false) }}
                    disabled={acting}
                    className="px-2.5 py-1 rounded-md text-[11px] font-semibold"
                    style={{ background: 'var(--tool-dim)', color: 'var(--tool)' }}
                  >
                    Confirm
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmRemove(false)}
                    className="px-2.5 py-1 rounded-md text-[11px]"
                    style={{ color: 'var(--text-muted)' }}
                  >
                    No
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirmRemove(true)}
                  className="text-[11px] px-2.5 py-1 rounded-md transition-all"
                  style={{ color: 'var(--text-muted)', border: '1px solid var(--border-subtle)' }}
                >
                  Remove
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Expanded node list */}
      {expanded && hitNodes.length > 0 && (
        <div
          className="px-5 pb-4"
        >
          <div
            className="rounded-lg px-3.5 py-2.5 flex flex-wrap gap-1.5"
            style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-default)' }}
          >
            {hitNodes.map((node) => (
              <span
                key={node}
                className="text-[10.5px] font-mono px-2 py-0.5 rounded-md"
                style={{
                  background: 'var(--iris-dim)',
                  border: '1px solid var(--iris-dim)',
                  color: 'var(--text-secondary)',
                }}
              >
                {node}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Compact Shared Signature Row ─────────────────────────────

export function SharedSignatureRow({
  sig,
  stats,
}: {
  sig: Signature
  stats: SignatureStatsData | null
}) {
  const [expanded, setExpanded] = useState(false)
  const hits = stats?.total_hits ?? sig.metadata.total_hits ?? 0
  const runsHit = stats?.runs_hit ?? 0
  const fpCount = stats?.false_positive_count ?? 0
  const lastHit = stats?.last_hit || sig.metadata.last_hit_at || ''
  const hitNodes = stats?.hit_nodes ?? []

  return (
    <div
      className="rounded-lg overflow-hidden transition-all cursor-pointer"
      style={{
        background: 'var(--bg-surface)',
        border: '1px solid var(--border-subtle)',
      }}
      onClick={() => setExpanded(!expanded)}
    >
      {/* Compact row */}
      <div className="px-4 py-3 flex items-center gap-3">
        {/* ID */}
        <span className="font-mono text-[10.5px] font-bold shrink-0" style={{ color: 'var(--iris)', minWidth: 72 }}>
          {sig.id}
        </span>

        {/* Strategy badge */}
        <StrategyBadge strategy={sig.match_strategy} />

        {/* Pattern — truncated */}
        <span
          className="font-mono text-[11.5px] truncate flex-1 min-w-0"
          style={{ color: 'var(--text-secondary)' }}
          title={sig.pattern}
        >
          {sig.pattern}
        </span>

        {/* Catch count */}
        <div className="flex items-center gap-1 shrink-0">
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
            <path d="M8 3v5l3.5 2" stroke={hits > 0 ? 'var(--ok)' : 'var(--text-faint)'} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
            <circle cx="8" cy="8" r="6.5" stroke={hits > 0 ? 'var(--ok)' : 'var(--text-faint)'} strokeWidth="1.3"/>
          </svg>
          <span className="text-[11.5px] font-semibold tabular-nums" style={{ color: hits > 0 ? 'var(--ok)' : 'var(--text-muted)' }}>
            {hits}
          </span>
        </div>

        {/* FP indicator */}
        {fpCount > 0 && (
          <div className="flex items-center gap-0.5 shrink-0">
            <svg width="10" height="10" viewBox="0 0 16 16" fill="none">
              <path d="M8 5v3.5M8 11h.01" stroke="var(--quality)" strokeWidth="1.4" strokeLinecap="round"/>
              <path d="M6.86 2.57L1.21 12.28a1.33 1.33 0 001.14 2h11.3a1.33 1.33 0 001.14-2L9.14 2.57a1.33 1.33 0 00-2.28 0z" stroke="var(--quality)" strokeWidth="1.2"/>
            </svg>
            <span className="text-[10.5px] font-medium" style={{ color: 'var(--quality)' }}>
              {fpCount}
            </span>
          </div>
        )}

        {/* Chevron */}
        <svg
          width="10" height="10" viewBox="0 0 16 16" fill="none"
          className="shrink-0"
          style={{ transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s', color: 'var(--text-faint)' }}
        >
          <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="px-4 pb-3 flex flex-col gap-2" style={{ borderTop: '1px solid var(--border-subtle)' }} onClick={(e) => e.stopPropagation()}>
          <div className="pt-2.5">
            {/* Full pattern */}
            <div
              className="rounded-md px-3 py-2 font-mono text-[12px] break-all"
              style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-default)', color: 'var(--text-primary)' }}
            >
              {sig.pattern}
            </div>
          </div>

          {/* Description */}
          <p className="text-[12px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
            {sig.description}
          </p>

          {/* Meta row */}
          <div className="flex items-center gap-3 flex-wrap text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
            <SeverityBadge severity={sig.severity} />
            {sig.metadata.contributed_by && (
              <span>by {sig.metadata.contributed_by}</span>
            )}
            {runsHit > 0 && (
              <span>across <strong style={{ color: 'var(--text-secondary)' }}>{runsHit}</strong> {runsHit === 1 ? 'run' : 'runs'}</span>
            )}
            {lastHit && (
              <span>last: {timeAgo(lastHit)}</span>
            )}
            {hits === 0 && (
              <span className="italic" style={{ color: 'var(--text-faint)' }}>No matches yet</span>
            )}
          </div>

          {/* Hit nodes */}
          {hitNodes.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {hitNodes.map((node) => (
                <span
                  key={node}
                  className="text-[10px] font-mono px-1.5 py-0.5 rounded"
                  style={{
                    background: 'var(--iris-dim)',
                    border: '1px solid var(--iris-dim)',
                    color: 'var(--text-secondary)',
                  }}
                >
                  {node}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main Page ────────────────────────────────────────────────
