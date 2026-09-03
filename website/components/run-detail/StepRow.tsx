'use client'

import { useState } from 'react'
import { Check, AlertTriangle, ArrowDown, RotateCcw, Play, X, ArrowRight, ChevronDown, FileCode, AlertCircle } from 'lucide-react'
import type { NodeEvent, RunRecord } from '@/lib/types'
import { getStepDisplay, getDetailLines, formatDur, fmtCost, SENTINEL_NODES } from '@/lib/run-utils'
import type { NodeDiffData } from './ReplayControls'
import JsonViewer from '../JsonViewer'

/* ── Result badge (pass / fail / degraded) ─────────────────────── */

function ResultBadge({ event }: { event: NodeEvent }) {
  const display = getStepDisplay(event)
  const status = event.status

  if (status === 'pass') {
    return (
      <span className="flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--success)' }}>
        <Check className="size-3.5" />
        {display.label}
        {display.warnSuffix && <span className="text-muted-foreground font-normal"> (warnings)</span>}
      </span>
    )
  }
  if (status === 'degraded_input') {
    return (
      <span className="flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--warning)' }}>
        <ArrowDown className="size-3.5" />
        {display.label}
      </span>
    )
  }
  if (status === 'skipped' || status === 'retried') {
    return (
      <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        {display.label}
      </span>
    )
  }
  if (status === 'interrupted') {
    return (
      <span className="flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--warning)' }}>
        {display.label}
      </span>
    )
  }
  return (
    <span className="flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--failure)' }}>
      <AlertTriangle className="size-3.5" />
      {display.label}
    </span>
  )
}

/* ── Inline Node Diff ──────────────────────────────────────────── */

function InlineNodeDiff({ diff, onDismiss }: { diff: NodeDiffData; onDismiss?: () => void }) {
  const origDisplay = getStepDisplay(diff.originalStep)
  const replayDisplay = getStepDisplay(diff.replayStep)
  const statusChanged = diff.originalStep.status !== diff.replayStep.status
  const origBad = ['crashed', 'fail', 'semantic_fail'].includes(diff.originalStep.status)
  const replayGood = diff.replayStep.status === 'pass'
  const replayBad = ['crashed', 'fail', 'semantic_fail'].includes(diff.replayStep.status)

  let verdictLabel = 'Changed'
  let verdictColor = 'var(--text-secondary)'
  if (statusChanged && origBad && replayGood) {
    verdictLabel = 'FIXED'
    verdictColor = 'var(--success)'
  } else if (statusChanged && replayBad) {
    verdictLabel = 'REGRESSION'
    verdictColor = 'var(--failure)'
  } else if (!statusChanged) {
    verdictLabel = 'Unchanged'
  }

  return (
    <div className="mt-3">
      <div className="flex items-center gap-2 py-2">
        <div className="flex-1 border-t-2 border-dashed" style={{ borderColor: 'color-mix(in srgb, var(--semantic) 25%, transparent)' }} />
        <span className="text-[10px] uppercase tracking-widest font-semibold shrink-0" style={{ color: 'var(--semantic)' }}>
          node rerun diff
        </span>
        <span className="text-[10px] font-bold px-1.5 py-0.5 rounded shrink-0" style={{ color: verdictColor, background: `color-mix(in srgb, ${verdictColor} 10%, transparent)` }}>
          {verdictLabel}
        </span>
        <div className="flex-1 border-t-2 border-dashed" style={{ borderColor: 'color-mix(in srgb, var(--semantic) 25%, transparent)' }} />
        {onDismiss && (
          <button type="button" onClick={onDismiss} className="p-0.5 rounded hover:bg-white/5 transition-colors shrink-0 text-muted-foreground">
            <X size={12} />
          </button>
        )}
      </div>

      <div className="rounded-[8px] border border-border bg-card overflow-hidden">
        <div className="grid grid-cols-2">
          {/* BEFORE */}
          <div className="p-3 border-r border-border">
            <div className="flex items-center gap-2 mb-2.5">
              <span className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary">Before</span>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded font-medium"
                style={{ background: `color-mix(in srgb, ${origDisplay.labelColor} 8%, transparent)`, color: origDisplay.labelColor, border: `1px solid color-mix(in srgb, ${origDisplay.labelColor} 20%, transparent)` }}>
                {origDisplay.icon} {diff.originalStep.status}
              </span>
            </div>
            {diff.originalStep.output_dict !== null ? (
              <div className="rounded p-2 text-[11px] max-h-[200px] overflow-auto border border-border bg-background">
                <JsonViewer data={diff.originalStep.output_dict} defaultCollapsed={false} />
              </div>
            ) : (
              <div className="text-[11px] italic text-text-tertiary">no output</div>
            )}
          </div>

          {/* AFTER */}
          <div className="p-3">
            <div className="flex items-center gap-2 mb-2.5">
              <span className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary">After</span>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded font-medium"
                style={{ background: `color-mix(in srgb, ${replayDisplay.labelColor} 8%, transparent)`, color: replayDisplay.labelColor, border: `1px solid color-mix(in srgb, ${replayDisplay.labelColor} 20%, transparent)` }}>
                {replayDisplay.icon} {diff.replayStep.status}
              </span>
            </div>
            {diff.replayStep.output_dict !== null ? (
              <div className="rounded p-2 text-[11px] max-h-[200px] overflow-auto border border-border bg-background">
                <JsonViewer data={diff.replayStep.output_dict} defaultCollapsed={false} />
              </div>
            ) : (
              <div className="text-[11px] italic text-text-tertiary">no output</div>
            )}
          </div>
        </div>

        {diff.aiSummary && (
          <div className="px-4 py-3 border-t border-border">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="h-3 w-[2px] rounded-full bg-primary" />
              <span className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary">
                AI Summary
              </span>
            </div>
            <p className="text-[12px] leading-relaxed text-muted-foreground pl-3">
              {diff.aiSummary}
            </p>
          </div>
        )}

        {statusChanged && (
          <div className="flex items-center justify-center gap-3 px-4 py-2 font-mono text-[11px] border-t border-border bg-card">
            <span style={{ color: origDisplay.labelColor }}>{origDisplay.icon} {diff.originalStep.status}</span>
            <ArrowRight size={12} className="text-muted-foreground" />
            <span style={{ color: replayDisplay.labelColor }}>{replayDisplay.icon} {diff.replayStep.status}</span>
            <span className="font-bold ml-1" style={{ color: verdictColor }}>{verdictLabel}</span>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Step Card (reference-matching card layout) ────────────────── */

export default function StepRow({
  event,
  nameCol,
  run,
  displayIndex,
  onReplay,
  onReplayNode,
  isReplaying,
  nodeDiff,
  onDismissDiff,
}: {
  event: NodeEvent
  nameCol: number
  run: RunRecord
  displayIndex?: number
  onReplay?: (node: string) => void
  onReplayNode?: (node: string) => void
  isReplaying?: boolean
  nodeDiff?: NodeDiffData
  onDismissDiff?: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const display = getStepDisplay(event)
  const details = getDetailLines(event, run)
  const number = (displayIndex ?? event.step_index) + 1
  const showActions = !SENTINEL_NODES.has(event.node_name)
  const isProblem = event.status !== 'pass'
  const isRootCause = run.root_cause_chain?.includes(event.node_name)

  return (
    <div
      className="rounded-[8px] border border-border bg-card p-4 cursor-pointer transition-colors hover:bg-card/80"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        {/* Left: step info */}
        <div className="flex items-start gap-3 min-w-0 flex-1">
          <span className="tnum font-mono text-sm text-text-tertiary shrink-0 mt-0.5">
            {String(number).padStart(2, '0')}
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm font-semibold text-foreground">{event.node_name}</span>
              <ResultBadge event={event} />
              {event.behavior_type && (
                <span className="rounded-[4px] bg-white/[0.05] px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  {event.behavior_type}
                </span>
              )}
              <span className="tnum font-mono text-[11px] text-text-tertiary">{formatDur(event.duration_ms)}</span>
              {event.llm_usage?.total_cost_usd != null && event.llm_usage.total_cost_usd > 0 && (
                <span className="tnum font-mono text-[11px] font-medium" style={{ color: 'var(--success)' }}>
                  {fmtCost(event.llm_usage.total_cost_usd)}
                </span>
              )}
              {/* Source file path */}
              {run.node_fn_paths?.[event.node_name] ? (
                <span
                  className="rounded-[4px] bg-white/[0.05] px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground flex items-center gap-1"
                  title={run.node_fn_refs?.[event.node_name] ?? undefined}
                >
                  <FileCode className="size-3 shrink-0" />
                  {run.node_fn_paths[event.node_name]}
                </span>
              ) : showActions ? (
                <span
                  className="flex items-center gap-1 text-[11px] text-muted-foreground"
                  title="Source not resolved — replay may not work for this node"
                >
                  <AlertCircle className="size-3" style={{ color: 'var(--warning)' }} />
                </span>
              ) : null}
            </div>

            {/* Root cause label */}
            {isRootCause && (
              <div className="mt-1.5">
                <span className="text-xs font-bold" style={{ color: 'var(--failure)' }}>root cause</span>
              </div>
            )}

            {/* Detail lines */}
            {details.length > 0 && (
              <ul className="mt-2 flex flex-col gap-1">
                {details.map((line, i) => (
                  <li
                    key={i}
                    className="text-[13px] leading-relaxed"
                    style={{
                      color: line.color ?? 'var(--text-secondary)',
                      fontStyle: line.italic ? 'italic' : undefined,
                      fontWeight: line.bold ? 700 : undefined,
                    }}
                  >
                    {line.text}
                  </li>
                ))}
              </ul>
            )}

            {/* Replaying spinner */}
            {isReplaying && (
              <div className="mt-2 flex items-center gap-1.5 text-[11px] font-medium" style={{ color: 'var(--semantic)' }}>
                <span className="inline-block w-3 h-3 border-2 border-purple-300 border-t-purple-600 rounded-full animate-spin" />
                replaying...
              </div>
            )}
          </div>
        </div>

        {/* Right: actions */}
        <div className="flex flex-col items-end gap-3 shrink-0">
          {(isProblem || run.node_fn_paths?.[event.node_name]) && showActions && (onReplayNode || onReplay) && !isReplaying && (
            <div className="flex items-center gap-2">
              {onReplayNode && (
                <button
                  className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
                  onClick={(e) => { e.stopPropagation(); onReplayNode(event.node_name) }}
                >
                  <RotateCcw className="size-3" />
                  Rerun Node
                </button>
              )}
              {onReplay && (
                <button
                  className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90"
                  onClick={(e) => { e.stopPropagation(); onReplay(event.node_name) }}
                >
                  <Play className="size-3" />
                  Rerun From Here
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Inline node diff */}
      {nodeDiff && (
        <InlineNodeDiff diff={nodeDiff} onDismiss={onDismissDiff} />
      )}

      {/* Expanded input/output */}
      {expanded && (
        <div className="mt-3 rounded-[8px] border border-border bg-background overflow-hidden" onClick={(e) => e.stopPropagation()}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-0">
            {event.input_state !== null && (
              <div className="p-3 border-r border-border">
                <div className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary mb-2">Input</div>
                <JsonViewer data={event.input_state} defaultCollapsed={true} />
              </div>
            )}
            {event.output_dict !== null && (
              <div className="p-3">
                <div className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary mb-2">Output</div>
                <JsonViewer data={event.output_dict} defaultCollapsed={true} />
              </div>
            )}
          </div>
          {event.semantic_check && (() => {
            const passed = event.semantic_check.passed
            const color = passed ? 'var(--success)' : 'var(--failure)'
            const confidencePct = Math.round(event.semantic_check.confidence * 100)
            return (
              <div className="mx-3 mb-3 rounded-md overflow-hidden"
                style={{ background: `color-mix(in srgb, ${color} 6%, transparent)`, border: `1px solid color-mix(in srgb, ${color} 15%, transparent)`, borderLeftWidth: '3px', borderLeftColor: color }}>
                <div className="flex items-center gap-3 px-3 py-2.5">
                  <span className="text-[11px] font-bold" style={{ color }}>{passed ? '\u2713' : '\u2717'}</span>
                  <span className="text-[10px] uppercase tracking-widest font-semibold text-text-tertiary">Node-Level Check</span>
                  <span className="text-[10px] font-mono font-semibold px-2 py-0.5 rounded-[4px] border"
                    style={{ color, backgroundColor: `color-mix(in srgb, ${color} 10%, transparent)`, borderColor: `color-mix(in srgb, ${color} 25%, transparent)` }}>
                    {passed ? 'COHERENT' : 'INCOHERENT'} {confidencePct}%
                  </span>
                </div>
                {event.semantic_check.reason && (
                  <div className="px-3 pb-2.5"><p className="text-[11px] leading-relaxed pl-6" style={{ color: passed ? 'var(--text-secondary)' : 'var(--failure)' }}>{event.semantic_check.reason}</p></div>
                )}
              </div>
            )
          })()}
          {event.exception && (
            <div className="p-3 border-t border-border">
              <div className="text-[10px] uppercase tracking-widest font-semibold mb-2" style={{ color: 'var(--failure)' }}>Full Exception</div>
              <pre className="text-[11px] rounded-[8px] p-3 overflow-x-auto whitespace-pre-wrap leading-5 font-mono border"
                style={{ color: 'var(--tool)', background: 'color-mix(in srgb, var(--failure) 4%, transparent)', borderColor: 'color-mix(in srgb, var(--failure) 15%, transparent)' }}>
                {event.exception}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
