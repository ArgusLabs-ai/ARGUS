'use client'

import { useState, useEffect } from 'react'
import type { RunRecord, LLMInvestigationResult } from '@/lib/types'
import { isReplayOf, runBLabel } from '../lib/compare-utils'

const C_GREEN = 'var(--ok)'
const C_AMBER = 'var(--quality)'
const C_RED = 'var(--tool)'
const C_INDIGO = 'var(--iris)'

function renderWithCode(text: string): (string | JSX.Element)[] {
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
        style={{ background: 'color-mix(in srgb, var(--primary) 6%, transparent)', color: 'var(--iris-bright)', border: '1px solid color-mix(in srgb, var(--primary) 12%, transparent)' }}
      >
        {code}
      </code>
    )
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts.length > 0 ? parts : [text]
}

function confColor(c: number) {
  return c >= 0.75 ? C_GREEN : c >= 0.45 ? C_AMBER : 'var(--ink-3)'
}

function AnalysisPanel({ inv, run, label }: { inv: LLMInvestigationResult; run: RunRecord; label: string }) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const hasError = !!inv.error
  const isHealthy = !hasError && run.overall_status === 'clean'
  const cc = confColor(inv.confidence)

  return (
    <div className="rounded-[10px] border border-border bg-card overflow-hidden" style={{ border: '1px solid color-mix(in srgb, var(--primary) 15%, transparent)' }}>
      {/* Header */}
      <div className="px-4 py-3 flex items-center gap-2.5" style={{ background: 'color-mix(in srgb, var(--primary) 4%, transparent)' }}>
        <div className="w-1 h-4 rounded-full" style={{ background: C_INDIGO }} />
        <span className="text-[12px] font-semibold" style={{ color: C_INDIGO }}>{label}</span>
        {!hasError && (
          <span
            className="ml-auto text-[11px] font-medium px-2 py-0.5 rounded-full"
            style={{ color: cc, background: `color-mix(in srgb, ${cc} 12%, transparent)`, border: `1px solid color-mix(in srgb, ${cc} 25%, transparent)` }}
          >
            {(inv.confidence * 100).toFixed(0)}% confidence
          </span>
        )}
      </div>

      <div className="px-4 pb-4 space-y-4">
        {/* Error */}
        {hasError && (
          <div className="mt-3 p-3 rounded-lg text-[12px]" style={{ background: 'color-mix(in srgb, var(--failure) 4%, transparent)', border: '1px solid color-mix(in srgb, var(--failure) 12%, transparent)' }}>
            <span className="font-semibold" style={{ color: C_RED }}>Analysis failed: </span>
            <span style={{ color: 'var(--tool)' }}>{inv.error}</span>
          </div>
        )}

        {/* Healthy */}
        {!hasError && isHealthy && inv.root_cause_explanation && (
          <div className="mt-3 p-4 rounded-lg" style={{ background: 'color-mix(in srgb, var(--success) 4%, transparent)', border: '1px solid color-mix(in srgb, var(--success) 12%, transparent)' }}>
            <div className="flex items-center gap-2 mb-2">
              <span style={{ color: C_GREEN }} className="text-base">{'\u2713'}</span>
              <span className="text-[11px] uppercase tracking-widest font-semibold" style={{ color: C_GREEN }}>Pipeline healthy</span>
            </div>
            <p className="text-[13px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>{inv.root_cause_explanation}</p>
          </div>
        )}

        {/* Failure analysis */}
        {!hasError && !isHealthy && (
          <div className="mt-3 space-y-4">
            {/* Root Cause Node */}
            {(run.root_cause_chain?.length > 0 || run.first_failure_step) && (
              <div>
                <span className="text-[12px] font-bold" style={{ color: 'var(--foreground)' }}>Root Cause Node:</span>
                <span
                  className="ml-2 text-[11px] font-mono px-2 py-0.5 rounded"
                  style={{ background: 'color-mix(in srgb, var(--failure) 8%, transparent)', color: C_RED, border: '1px solid color-mix(in srgb, var(--failure) 20%, transparent)' }}
                >
                  {run.first_failure_step ?? run.root_cause_chain?.[0]}
                </span>
              </div>
            )}

            {/* Root Cause Reason */}
            {inv.root_cause_explanation && (
              <div>
                <p className="text-[12px] font-bold mb-1" style={{ color: 'var(--foreground)' }}>Root Cause:</p>
                <p className="text-[12.5px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>{inv.root_cause_explanation}</p>
              </div>
            )}

            {/* Narrative */}
            {inv.degradation_narrative && (
              <div>
                <p className="text-[12px] font-bold mb-1" style={{ color: 'var(--foreground)' }}>What Happened:</p>
                <p className="text-[12.5px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>{inv.degradation_narrative}</p>
              </div>
            )}

            {/* Suggested Fixes */}
            {inv.debugging_suggestions.length > 0 && (
              <div>
                <p className="text-[12px] font-bold mb-2" style={{ color: 'var(--foreground)' }}>Suggested Fixes:</p>
                <div className="space-y-2">
                  {inv.debugging_suggestions.map((s, i) => {
                    const nodeMatch = s.match(/^\[([^\]]+)\]\s*/)
                    const nodeName = nodeMatch ? nodeMatch[1] : null
                    const rest = nodeName ? s.slice(nodeMatch![0].length) : s
                    const codeIdx = rest.indexOf('\n    ')
                    let description: string
                    let codePart: string | null
                    if (codeIdx >= 0) {
                      description = rest.slice(0, codeIdx)
                      codePart = rest.slice(codeIdx).trim()
                    } else {
                      description = rest
                      codePart = null
                    }

                    return (
                      <div key={i} className="p-3 rounded-lg" style={{ background: 'var(--card)', border: '1px solid var(--border)' }}>
                        <div className="flex items-start gap-2">
                          <span
                            className="shrink-0 w-4 h-4 rounded-full flex items-center justify-center text-[9px] font-bold mt-0.5"
                            style={{ background: `color-mix(in srgb, ${C_GREEN} 15%, transparent)`, color: C_GREEN, border: `1px solid color-mix(in srgb, ${C_GREEN} 30%, transparent)` }}
                          >
                            {i + 1}
                          </span>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-start gap-1.5 flex-wrap">
                              {nodeName && (
                                <span
                                  className="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded"
                                  style={{ background: 'color-mix(in srgb, var(--primary) 10%, transparent)', color: 'var(--semantic)', border: '1px solid color-mix(in srgb, var(--semantic) 20%, transparent)' }}
                                >
                                  {nodeName}
                                </span>
                              )}
                              <span className="text-[12px] font-semibold leading-snug" style={{ color: 'var(--foreground)' }}>
                                {renderWithCode(description)}
                              </span>
                            </div>
                            {codePart && (
                              <pre
                                className="mt-2 px-3 py-2 rounded-lg text-[11px] font-mono overflow-x-auto whitespace-pre-wrap"
                                style={{ background: 'var(--fill-subtle)', color: 'var(--iris-bright)', border: '1px solid color-mix(in srgb, var(--primary) 10%, transparent)' }}
                              >
                                {codePart}
                              </pre>
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

        {/* Causal Hypotheses */}
        {!hasError && inv.causal_hypotheses.length > 0 && (
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: '0.75rem' }}>
            <button
              type="button"
              onClick={() => setDetailsOpen(!detailsOpen)}
              className="flex items-center gap-2 text-[11px] hover:opacity-80 transition-opacity"
              style={{ color: 'var(--text-tertiary)' }}
            >
              <span className="text-[10px]">{detailsOpen ? '\u25BE' : '\u25B8'}</span>
              <span>Causal Hypotheses ({inv.causal_hypotheses.length})</span>
              {inv.observations.length > 0 && (
                <span className="ml-1">&middot; {inv.observations.length} observations</span>
              )}
            </button>

            {detailsOpen && (
              <div className="mt-3 space-y-2">
                {inv.causal_hypotheses.map((h, i) => {
                  const hc = confColor(h.confidence)
                  return (
                    <div key={i} className="p-3 rounded-lg" style={{ background: 'var(--card)', border: '1px solid var(--border)' }}>
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-full" style={{ color: hc, background: `color-mix(in srgb, ${hc} 12%, transparent)` }}>
                          {(h.confidence * 100).toFixed(0)}%
                        </span>
                        <span className="px-1.5 py-0.5 rounded-full text-[9px] font-medium" style={{ background: 'color-mix(in srgb, var(--primary) 6%, transparent)', color: C_INDIGO }}>
                          {h.category}
                        </span>
                      </div>
                      <div className="text-[12px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>{h.hypothesis}</div>
                      {h.supporting_evidence.length > 0 && (
                        <div className="mt-1.5 text-[11px]" style={{ color: 'var(--text-tertiary)' }}>
                          Evidence: {h.supporting_evidence.join(', ')}
                        </div>
                      )}
                    </div>
                  )
                })}

                {inv.observations.length > 0 && (
                  <div className="pt-2">
                    <div className="text-[10px] uppercase tracking-widest font-semibold mb-1.5" style={{ color: 'var(--text-tertiary)' }}>Observations</div>
                    <div className="space-y-1">
                      {inv.observations.map((o, i) => (
                        <div key={i} className="flex items-baseline gap-2 text-[11px]">
                          <span style={{ color: 'var(--text-tertiary)' }}>&middot;</span>
                          <span style={{ color: 'var(--text-secondary)' }}>{o}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Meta */}
        {!hasError && (
          <div className="flex items-center gap-4 pt-2 text-[10px] font-mono" style={{ color: 'var(--text-tertiary)', borderTop: '1px solid var(--border)' }}>
            {inv.prompt_tokens > 0 && <span>{inv.prompt_tokens + inv.completion_tokens} tokens</span>}
            {inv.investigation_duration_ms > 0 && <span>{(inv.investigation_duration_ms / 1000).toFixed(1)}s</span>}
          </div>
        )}
      </div>
    </div>
  )
}

function ComparativeSummary({ invA, invB, runA, runB, bLabel }: { invA: LLMInvestigationResult; invB: LLMInvestigationResult; runA: RunRecord; runB: RunRecord; bLabel: string }) {
  const confDelta = invB.confidence - invA.confidence
  const aFailed = runA.overall_status !== 'clean'
  const bFailed = runB.overall_status !== 'clean'
  const fixed = aFailed && !bFailed
  const regressed = !aFailed && bFailed
  const bothFailed = aFailed && bFailed

  const rootCauseChanged = invA.root_cause_explanation !== invB.root_cause_explanation
  const fixCountA = invA.debugging_suggestions?.length ?? 0
  const fixCountB = invB.debugging_suggestions?.length ?? 0

  return (
    <div className="rounded-[10px] border border-border bg-card overflow-hidden" style={{ border: `1px solid ${fixed ? 'color-mix(in srgb, var(--success) 20%, transparent)' : regressed ? 'color-mix(in srgb, var(--failure) 20%, transparent)' : 'var(--border)'}` }}>
      <div
        className="px-4 py-3 flex items-center gap-2.5"
        style={{ background: fixed ? 'color-mix(in srgb, var(--success) 4%, transparent)' : regressed ? 'color-mix(in srgb, var(--failure) 4%, transparent)' : 'var(--card)' }}
      >
        <span className="text-[14px]">{fixed ? '\u2713' : regressed ? '\u2717' : '\u2194'}</span>
        <span className="text-[13px] font-bold" style={{ color: fixed ? C_GREEN : regressed ? C_RED : 'var(--foreground)' }}>
          {fixed ? `Issue Resolved in ${bLabel}` : regressed ? 'Regression Detected' : 'Comparative Summary'}
        </span>
      </div>

      <div className="px-4 py-3 space-y-3">
        {/* Status transition */}
        <div className="flex items-center gap-2 text-[12px]">
          <span className="font-medium" style={{ color: 'var(--text-tertiary)' }}>Status:</span>
          <span
            className="px-2 py-0.5 rounded-full text-[11px] font-semibold"
            style={{ color: aFailed ? C_RED : C_GREEN, background: aFailed ? `color-mix(in srgb, ${C_RED} 10%, transparent)` : `color-mix(in srgb, ${C_GREEN} 10%, transparent)` }}
          >
            {runA.overall_status}
          </span>
          <span style={{ color: 'var(--text-tertiary)' }}>{'\u2192'}</span>
          <span
            className="px-2 py-0.5 rounded-full text-[11px] font-semibold"
            style={{ color: bFailed ? C_RED : C_GREEN, background: bFailed ? `color-mix(in srgb, ${C_RED} 10%, transparent)` : `color-mix(in srgb, ${C_GREEN} 10%, transparent)` }}
          >
            {runB.overall_status}
          </span>
        </div>

        {/* Confidence delta */}
        <div className="flex items-center gap-2 text-[12px]">
          <span className="font-medium" style={{ color: 'var(--text-tertiary)' }}>Confidence:</span>
          <span className="font-mono font-bold" style={{ color: confColor(invA.confidence) }}>
            {(invA.confidence * 100).toFixed(0)}%
          </span>
          <span style={{ color: 'var(--text-tertiary)' }}>{'\u2192'}</span>
          <span className="font-mono font-bold" style={{ color: confColor(invB.confidence) }}>
            {(invB.confidence * 100).toFixed(0)}%
          </span>
          {confDelta !== 0 && (
            <span className="text-[11px] font-medium" style={{ color: confDelta > 0 ? C_GREEN : C_RED }}>
              ({confDelta > 0 ? '+' : ''}{(confDelta * 100).toFixed(0)}%)
            </span>
          )}
        </div>

        {/* Root cause change */}
        {rootCauseChanged && bothFailed && (
          <div className="text-[12px]">
            <span className="font-medium" style={{ color: 'var(--text-tertiary)' }}>Root cause changed between runs</span>
          </div>
        )}

        {/* Fixes comparison */}
        {(fixCountA > 0 || fixCountB > 0) && (
          <div className="flex items-center gap-2 text-[12px]">
            <span className="font-medium" style={{ color: 'var(--text-tertiary)' }}>Suggested fixes:</span>
            <span style={{ color: 'var(--text-secondary)' }}>{fixCountA} (base)</span>
            <span style={{ color: 'var(--text-tertiary)' }}>{'\u2192'}</span>
            <span style={{ color: 'var(--text-secondary)' }}>{fixCountB} ({bLabel.toLowerCase()})</span>
          </div>
        )}

        {/* Trigger reasons diff */}
        {invA.trigger_reasons?.length > 0 && (
          <div className="text-[12px]">
            <span className="font-medium" style={{ color: 'var(--text-tertiary)' }}>Base triggers: </span>
            <span style={{ color: 'var(--text-secondary)' }}>{invA.trigger_reasons.join(', ')}</span>
          </div>
        )}
        {invB.trigger_reasons?.length > 0 && (
          <div className="text-[12px]">
            <span className="font-medium" style={{ color: 'var(--text-tertiary)' }}>{bLabel} triggers: </span>
            <span style={{ color: 'var(--text-secondary)' }}>{invB.trigger_reasons.join(', ')}</span>
          </div>
        )}
      </div>
    </div>
  )
}

interface CompareAnalysisResult {
  structural_summary: string
  performance_comparison: string
  failure_analysis: string
  root_cause_delta: string
  key_insights: string[]
  recommendation: string
  confidence: number
  error?: string | null
}

function CompareAnalysisCard({ analysis }: { analysis: CompareAnalysisResult }) {
  const cc = confColor(analysis.confidence)
  const sections = [
    { label: 'Structural Summary', content: analysis.structural_summary },
    { label: 'Performance', content: analysis.performance_comparison },
    { label: 'Failure Analysis', content: analysis.failure_analysis },
    { label: 'Root Cause Delta', content: analysis.root_cause_delta },
    { label: 'Recommendation', content: analysis.recommendation },
  ].filter((s) => s.content)

  return (
    <div className="rounded-[10px] border border-border bg-card overflow-hidden" style={{ border: '1px solid color-mix(in srgb, var(--primary) 20%, transparent)' }}>
      <div className="px-4 py-3 flex items-center gap-2.5" style={{ background: 'color-mix(in srgb, var(--primary) 6%, transparent)' }}>
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M8 1l1.5 3.5L13 6l-3 2 .5 4L8 10.5 5.5 12l.5-4-3-2 3.5-1.5L8 1Z" fill="color-mix(in srgb, var(--primary) 15%, transparent)" stroke="var(--iris)" strokeWidth="1"/>
        </svg>
        <span className="text-[13px] font-bold" style={{ color: C_INDIGO }}>Comparative AI Analysis</span>
        <span
          className="ml-auto text-[11px] font-medium px-2 py-0.5 rounded-full"
          style={{ color: cc, background: `color-mix(in srgb, ${cc} 12%, transparent)`, border: `1px solid color-mix(in srgb, ${cc} 25%, transparent)` }}
        >
          {(analysis.confidence * 100).toFixed(0)}% confidence
        </span>
      </div>

      <div className="px-4 pb-4 space-y-3.5">
        {sections.map((s) => (
          <div key={s.label} className="mt-3">
            <p className="text-[12px] font-bold mb-1" style={{ color: 'var(--foreground)' }}>{s.label}</p>
            <p className="text-[12.5px] leading-relaxed" style={{ color: 'var(--text-secondary)' }}>{renderWithCode(s.content!)}</p>
          </div>
        ))}

        {analysis.key_insights.length > 0 && (
          <div className="mt-3">
            <p className="text-[12px] font-bold mb-2" style={{ color: 'var(--foreground)' }}>Key Insights</p>
            <div className="space-y-1.5">
              {analysis.key_insights.map((insight, i) => (
                <div key={i} className="flex items-start gap-2 text-[12px]">
                  <span
                    className="shrink-0 w-4 h-4 rounded-full flex items-center justify-center text-[9px] font-bold mt-0.5"
                    style={{ background: `color-mix(in srgb, ${C_INDIGO} 15%, transparent)`, color: C_INDIGO }}
                  >
                    {i + 1}
                  </span>
                  <span className="leading-relaxed" style={{ color: 'var(--text-secondary)' }}>{renderWithCode(insight)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export default function AIAnalysisTab({ runA, runB, isLocal = false }: { runA: RunRecord; runB: RunRecord; isLocal?: boolean }) {
  const invA = runA.llm_investigation
  const invB = runB.llm_investigation
  const bLabel = runBLabel(runA, runB)

  const [compareAnalysis, setCompareAnalysis] = useState<CompareAnalysisResult | null>(null)
  const [compareLoading, setCompareLoading] = useState(false)
  const [compareError, setCompareError] = useState<string | null>(null)

  useEffect(() => {
    if (!isLocal) return
    setCompareLoading(true)
    setCompareError(null)
    fetch('/api/compare-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ a: runA.run_id, b: runB.run_id }),
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data: CompareAnalysisResult) => {
        if (data.error) setCompareError(data.error)
        else setCompareAnalysis(data)
      })
      .catch((e) => setCompareError(e.message))
      .finally(() => setCompareLoading(false))
  }, [runA.run_id, runB.run_id, isLocal])

  const bothTriggered = invA?.triggered && invB?.triggered
  const noPerRunAnalysis = !invA?.triggered && !invB?.triggered

  return (
    <div className="py-4 space-y-4">
      {/* Compare-specific AI analysis */}
      {compareLoading && (
        <div className="rounded-[10px] border border-border bg-card p-8 flex flex-col items-center justify-center gap-3" style={{ border: '1px solid color-mix(in srgb, var(--primary) 15%, transparent)' }}>
          <span className="inline-block w-5 h-5 border-2 rounded-full animate-spin" style={{ borderColor: 'color-mix(in srgb, var(--primary) 20%, transparent)', borderTopColor: 'var(--iris)' }} />
          <p className="text-[12px] font-medium" style={{ color: 'var(--text-tertiary)' }}>Generating comparative analysis...</p>
        </div>
      )}

      {compareError && (
        <div className="rounded-[10px] border border-border bg-card p-4" style={{ border: '1px solid color-mix(in srgb, var(--failure) 15%, transparent)' }}>
          <p className="text-[12px]" style={{ color: C_RED }}>
            Compare analysis unavailable: {compareError}
          </p>
          <p className="text-[11px] mt-1" style={{ color: 'var(--text-tertiary)' }}>
            Ensure a provider key is set (argus key set) and argus ui is running locally.
          </p>
        </div>
      )}

      {compareAnalysis && <CompareAnalysisCard analysis={compareAnalysis} />}

      {/* Comparative summary when both have per-run analysis */}
      {bothTriggered && (
        <ComparativeSummary invA={invA!} invB={invB!} runA={runA} runB={runB} bLabel={bLabel} />
      )}

      {/* Side-by-side per-run analysis panels */}
      {!noPerRunAnalysis && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {invA?.triggered ? (
            <AnalysisPanel inv={invA} run={runA} label="Base Run Analysis" />
          ) : (
            <div className="rounded-[10px] border border-border bg-card p-6 flex items-center justify-center">
              <p className="text-[12px]" style={{ color: 'var(--text-tertiary)' }}>No AI analysis triggered for the base run.</p>
            </div>
          )}
          {invB?.triggered ? (
            <AnalysisPanel inv={invB} run={runB} label={`${bLabel} Analysis`} />
          ) : (
            <div className="rounded-[10px] border border-border bg-card p-6 flex items-center justify-center">
              <p className="text-[12px]" style={{ color: 'var(--text-tertiary)' }}>No AI analysis triggered for {bLabel.toLowerCase()}.</p>
            </div>
          )}
        </div>
      )}

      {/* Empty state — only if no compare analysis AND no per-run analysis */}
      {noPerRunAnalysis && !compareAnalysis && !compareLoading && !compareError && (
        <div className="py-16 text-center">
          <div className="text-[28px] mb-3" style={{ color: 'var(--text-tertiary)' }}>{'\u2B50'}</div>
          <p className="text-[13px] font-medium" style={{ color: 'var(--text-tertiary)' }}>No AI analysis available for these runs.</p>
          <p className="text-[11px] mt-1" style={{ color: 'var(--text-tertiary)' }}>
            AI analysis is triggered automatically when failures are detected during a run.
          </p>
        </div>
      )}

      {/* Suggested signatures comparison */}
      {bothTriggered && ((invA!.suggested_signatures?.length ?? 0) > 0 || (invB!.suggested_signatures?.length ?? 0) > 0) && (
        <div className="rounded-[10px] border border-border bg-card overflow-hidden">
          <div className="px-4 py-2.5" style={{ background: 'var(--card)', borderBottom: '1px solid var(--border)' }}>
            <span className="text-[12px] font-bold" style={{ color: 'var(--foreground)' }}>Suggested Failure Signatures</span>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-0" style={{ borderTop: '1px solid var(--border)' }}>
            {[{ sigs: invA!.suggested_signatures ?? [], label: 'Base Run' }, { sigs: invB!.suggested_signatures ?? [], label: bLabel }].map(({ sigs, label }) => (
              <div key={label} className="p-3" style={{ borderRight: '1px solid var(--border)' }}>
                <div className="text-[10px] uppercase tracking-widest font-semibold mb-2" style={{ color: 'var(--text-tertiary)' }}>{label}</div>
                {sigs.length === 0 ? (
                  <p className="text-[11px]" style={{ color: 'var(--text-tertiary)' }}>None suggested</p>
                ) : (
                  <div className="space-y-2">
                    {sigs.map((sig, i) => (
                      <div key={i} className="p-2 rounded-lg text-[11px]" style={{ background: 'var(--card)', border: '1px solid var(--border)' }}>
                        <div className="flex items-center gap-2 mb-1">
                          <span
                            className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase"
                            style={{
                              color: sig.severity === 'critical' ? C_RED : C_AMBER,
                              background: sig.severity === 'critical' ? `color-mix(in srgb, ${C_RED} 10%, transparent)` : `color-mix(in srgb, ${C_AMBER} 10%, transparent)`,
                            }}
                          >
                            {sig.severity}
                          </span>
                          <span className="font-mono font-medium" style={{ color: C_INDIGO }}>{sig.match_strategy}</span>
                        </div>
                        <p style={{ color: 'var(--text-secondary)' }}>{sig.description}</p>
                        <code className="block mt-1 text-[10px] font-mono" style={{ color: 'var(--text-tertiary)' }}>{sig.pattern}</code>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
