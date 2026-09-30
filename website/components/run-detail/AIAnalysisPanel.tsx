'use client'

import { useState } from 'react'
import type { RunRecord } from '@/lib/types'

/** Render text with inline code spans highlighted */
function renderWithCode(text: string): (string | JSX.Element)[] {
  // Match: `backtick code`, function_name(...), UPPER_SNAKE, module.attr patterns
  const codeRe = /`([^`]+)`|(\b[a-z_]\w*\s*\([^)]*\))|(\b[A-Z][A-Z0-9_]{2,}\b)|(\b\w+\.\w+(?:\.\w+)+\b)/g
  const parts: (string | JSX.Element)[] = []
  let last = 0
  let m: RegExpExecArray | null

  while ((m = codeRe.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const code = m[1] ?? m[0]
    parts.push(
      <code
        key={m.index}
        className="text-[12px] font-mono px-1.5 py-0.5 rounded"
        style={{
          background: 'var(--iris-dim)',
          color: 'var(--primary)',
          border: '1px solid var(--iris-dim)',
        }}
      >
        {code}
      </code>
    )
    last = m.index + m[0].length
  }

  if (last < text.length) parts.push(text.slice(last))
  return parts.length > 0 ? parts : [text]
}

export default function AIAnalysisPanel({ run }: { run: RunRecord }) {
  const inv = run.llm_investigation
  const rc = run.replay_comparison
  const [detailsOpen, setDetailsOpen] = useState(false)

  const hasComparison = rc && !rc.error
  const hasInvestigation = inv && inv.triggered

  if (!hasInvestigation && !hasComparison) return null

  // Use comparison confidence for replay runs, investigation confidence otherwise
  const confidence = hasComparison ? rc.confidence : (inv?.confidence ?? 0)
  const hasError = hasInvestigation ? !!inv.error : false
  const isHealthy = !hasError && run.overall_status === 'clean'

  const confColor =
    confidence >= 0.75 ? 'var(--success)' : confidence >= 0.45 ? 'var(--warning)' : 'var(--muted-foreground)'

  return (
    <div className="rounded-[10px] border border-border bg-card p-5">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="h-4 w-[3px] rounded-full bg-primary" />
          <h2 className="text-[15px] font-semibold text-foreground">
            {hasComparison ? 'Replay Analysis' : 'AI Analysis'}
          </h2>
        </div>
        <span
          className="text-[11px] font-medium px-2 py-0.5 rounded-[4px] border"
          style={{
            color: confColor,
            backgroundColor: `color-mix(in srgb, ${confColor} 10%, transparent)`,
            borderColor: confColor,
          }}
        >
          {(confidence * 100).toFixed(0)}% confidence
        </span>
      </div>

      {/* Replay comparison section */}
      {hasComparison && (
        <div className="mt-4 space-y-4">
          {/* Root cause delta */}
          {rc.root_cause_delta && (
            <div className="p-4 rounded-lg" style={{ background: 'color-mix(in srgb, var(--primary) 4%, transparent)', border: '1px solid color-mix(in srgb, var(--primary) 12%, transparent)' }}>
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] uppercase tracking-widest font-semibold" style={{ color: 'var(--primary)' }}>
                  What Changed
                </span>
              </div>
              <p className="text-[13px] leading-relaxed text-muted-foreground">
                {renderWithCode(rc.root_cause_delta)}
              </p>
            </div>
          )}

          {/* Failure analysis */}
          {rc.failure_analysis && (
            <div className="p-4 rounded-lg" style={{ background: 'color-mix(in srgb, var(--foreground) 2%, transparent)', border: '1px solid var(--border)' }}>
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary">
                  Failure Analysis
                </span>
              </div>
              <p className="text-[13px] leading-relaxed text-muted-foreground">
                {renderWithCode(rc.failure_analysis)}
              </p>
            </div>
          )}

          {/* Key insights */}
          {rc.key_insights?.length > 0 && (
            <div>
              <span className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary">
                Key Insights
              </span>
              <ul className="mt-2 space-y-1.5">
                {rc.key_insights.map((insight, i) => (
                  <li key={i} className="flex items-start gap-2 text-[13px] text-muted-foreground">
                    <span className="text-primary mt-0.5 shrink-0">{'•'}</span>
                    <span className="leading-relaxed">{renderWithCode(insight)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Recommendation */}
          {rc.recommendation && (
            <div className="p-3 rounded-lg" style={{ background: 'color-mix(in srgb, var(--success) 4%, transparent)', border: '1px solid color-mix(in srgb, var(--success) 12%, transparent)' }}>
              <span className="text-[10px] uppercase tracking-widest font-semibold" style={{ color: 'var(--success)' }}>
                Recommendation
              </span>
              <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">
                {renderWithCode(rc.recommendation)}
              </p>
            </div>
          )}
        </div>
      )}

      <div className="mt-4 flex flex-col gap-4">

        {/* Error state */}
        {hasError && (
          <div className="mt-4 p-4 rounded-lg text-[13px]"
            style={{ background: 'color-mix(in srgb, var(--failure) 4%, transparent)', border: '1px solid color-mix(in srgb, var(--failure) 12%, transparent)' }}>
            <span className="font-semibold" style={{ color: 'var(--failure)' }}>Analysis failed: </span>
            <span style={{ color: 'var(--failure)' }}>{inv?.error}</span>
          </div>
        )}

        {/* Healthy run */}
        {!hasError && isHealthy && inv?.root_cause_explanation && (
          <div className="mt-4 p-5 rounded-xl"
            style={{ background: 'color-mix(in srgb, var(--success) 4%, transparent)', border: '1px solid color-mix(in srgb, var(--success) 12%, transparent)' }}>
            <div className="flex items-center gap-2.5 mb-3">
              <span style={{ color: 'var(--success)' }} className="text-lg">{'\u2713'}</span>
              <span className="text-[12px] uppercase tracking-widest font-semibold" style={{ color: 'var(--success)' }}>
                Pipeline healthy
              </span>
            </div>
            <p className="text-[14px] leading-relaxed" style={{ color: 'var(--ink-2)', maxWidth: '680px' }}>
              {inv?.root_cause_explanation}
            </p>
          </div>
        )}

        {/* Root cause breakdown */}
        {!hasError && !isHealthy && (
          <div className="mt-4 space-y-5">

            {/* Root Cause Node */}
            {(run.first_failure_step || run.root_cause_chain?.length > 0) && (
              <div className="flex items-center gap-2">
                <span className="text-[13px] text-text-tertiary">
                  Root Cause Node:
                </span>
                <span
                  className="font-mono text-xs px-1.5 py-0.5 rounded-[4px] border"
                  style={{ backgroundColor: 'color-mix(in srgb, var(--failure) 10%, transparent)', color: 'var(--failure)', borderColor: 'color-mix(in srgb, var(--failure) 40%, transparent)' }}
                >
                  {run.first_failure_step ?? run.root_cause_chain?.[0]}
                </span>
              </div>
            )}

            {/* Root Cause Reason */}
            {inv?.root_cause_explanation && (
              <div>
                <p className="text-[13px] text-text-tertiary">
                  Root Cause Reason:
                </p>
                <p className="mt-1.5 text-sm leading-relaxed text-[var(--ink-2)]" style={{ maxWidth: '680px' }}>
                  {inv?.root_cause_explanation}
                </p>
              </div>
            )}

            {/* Forensic Narrative */}
            {inv?.degradation_narrative && (
              <div>
                <p className="text-[13px] text-text-tertiary">
                  What Happened:
                </p>
                <p className="mt-1.5 text-sm leading-relaxed text-[var(--ink-2)]" style={{ maxWidth: '680px' }}>
                  {inv?.degradation_narrative}
                </p>
              </div>
            )}

            {/* Fix */}
            {(inv?.debugging_suggestions?.length ?? 0) > 0 && (
              <div>
                <p className="text-[13px] text-text-tertiary">
                  Suggested Fixes:
                </p>
                <div className="mt-2 space-y-3">
                  {inv!.debugging_suggestions.map((s, i) => {
                    // Parse: "[node] what — why\n    code_hint"
                    // Also handles: "[node] what. — why." or plain text
                    const nodeMatch = s.match(/^\[([^\]]+)\]\s*/)
                    const nodeName = nodeMatch ? nodeMatch[1] : null
                    const rest = nodeName ? s.slice(nodeMatch![0].length) : s

                    // Extract code block: newline-indented or after "add:" / "example:" / "e.g."
                    const codeIdx = rest.indexOf('\n    ')
                    const inlineCodeMatch = codeIdx < 0
                      ? rest.match(/\b(add:|example:|e\.g\.\s*|such as:\s*|try:\s*)(.+)$/i)
                      : null

                    let description: string
                    let codePart: string | null

                    if (codeIdx >= 0) {
                      description = rest.slice(0, codeIdx)
                      codePart = rest.slice(codeIdx).trim()
                    } else if (inlineCodeMatch) {
                      description = rest.slice(0, inlineCodeMatch.index).trimEnd().replace(/[.,]\s*$/, '')
                      codePart = inlineCodeMatch[2].trim()
                    } else {
                      description = rest
                      codePart = null
                    }

                    // Split on " — " or " - " (em-dash or regular dash with spaces)
                    const emDashIdx = description.indexOf(' \u2014 ')
                    const regDashIdx = emDashIdx < 0 ? description.indexOf(' - ') : -1
                    const dashIdx = emDashIdx >= 0 ? emDashIdx : regDashIdx
                    const dashLen = emDashIdx >= 0 ? 3 : 3

                    const whatPart = dashIdx >= 0 ? description.slice(0, dashIdx) : description
                    const whyPart = dashIdx >= 0 ? description.slice(dashIdx + dashLen) : null

                    return (
                      <div
                        key={i}
                        className="p-4 rounded-[8px]"
                        style={{ background: 'var(--background)', border: '1px solid var(--border)' }}
                      >
                        <div className="flex items-start gap-3">
                          <span
                            className="shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-bold mt-0.5 bg-primary text-white"
                          >
                            {i + 1}
                          </span>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-start gap-2 flex-wrap mb-1">
                              {nodeName && (
                                <span
                                  className="inline-flex items-center shrink-0 text-[11px] font-mono font-bold px-2 py-0.5 rounded mt-0.5 text-muted-foreground"
                                  style={{
                                    background: 'var(--hover)',
                                  }}
                                >
                                  {nodeName}
                                </span>
                              )}
                              <span className="text-sm font-semibold leading-snug text-foreground">
                                {renderWithCode(whatPart)}
                              </span>
                            </div>
                            {whyPart && (
                              <p className="text-[13px] leading-relaxed mt-1.5 text-muted-foreground">
                                {renderWithCode(whyPart)}
                              </p>
                            )}
                            {codePart && (
                              <div className="mt-3 overflow-hidden rounded-[8px] border border-border bg-code">
                                <pre className="scrollbar-thin overflow-x-auto px-4 py-3 font-mono text-[12.5px] leading-relaxed">
                                  <code className="text-[var(--ink-2)]">{codePart}</code>
                                </pre>
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

          </div>
        )}

        {/* Causal hypotheses */}
        {!hasError && (inv?.causal_hypotheses?.length ?? 0) > 0 && (
          <div className="border-t border-border pt-5">
            <button
              type="button"
              onClick={() => setDetailsOpen(!detailsOpen)}
              className="flex items-center gap-2 text-[12px] hover:opacity-80 transition-opacity text-muted-foreground"
            >
              <span className="text-[10px]">{detailsOpen ? '\u25BE' : '\u25B8'}</span>
              <span>Causal Hypotheses ({inv!.causal_hypotheses.length})</span>
              {(inv?.observations?.length ?? 0) > 0 && (
                <span className="ml-1 text-muted-foreground">&middot; {inv!.observations.length} observation{inv!.observations.length !== 1 ? 's' : ''}</span>
              )}
            </button>

            {detailsOpen && (
              <div className="mt-4 space-y-3">
                {inv!.causal_hypotheses.map((h, i) => {
                  const hc = h.confidence >= 0.7 ? 'var(--success)' : h.confidence >= 0.4 ? 'var(--warning)' : 'var(--muted-foreground)'
                  return (
                    <div
                      key={i}
                      className="p-4 rounded-lg"
                      style={{ background: 'var(--card)', border: '1px solid var(--border)' }}
                    >
                      <div className="flex items-center gap-2.5 mb-2">
                        <span
                          className="text-[11px] font-bold px-2 py-0.5 rounded-full"
                          style={{
                            color: hc,
                            backgroundColor: `color-mix(in srgb, ${hc} 10%, transparent)`,
                          }}
                        >
                          {(h.confidence * 100).toFixed(0)}%
                        </span>
                        <span
                          className="px-2 py-0.5 rounded-full text-[10px] font-medium"
                          style={{ background: 'var(--iris-dim)', color: 'var(--primary)' }}
                        >
                          {h.category}
                        </span>
                      </div>
                      <div className="text-[13px] leading-relaxed text-muted-foreground">{h.hypothesis}</div>
                      {h.supporting_evidence.length > 0 && (
                        <div className="mt-2 text-[12px] text-muted-foreground">
                          Evidence: {h.supporting_evidence.join(', ')}
                        </div>
                      )}
                    </div>
                  )
                })}

                {(inv?.observations?.length ?? 0) > 0 && (
                  <div className="pt-3">
                    <div className="text-[10px] uppercase tracking-widest font-semibold mb-2 text-muted-foreground">Observations</div>
                    <div className="space-y-1.5">
                      {inv!.observations.map((o, i) => (
                        <div key={i} className="flex items-baseline gap-2 text-[12px]">
                          <span className="text-muted-foreground">&middot;</span>
                          <span className="text-muted-foreground">{o}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
