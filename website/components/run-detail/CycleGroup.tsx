'use client'

import { useState } from 'react'
import type { NodeEvent, RunRecord, LoopAnalysisResult } from '@/lib/types'
import type { NodeDiffData } from './ReplayControls'
import StepRow from './StepRow'

const C_GREEN = 'var(--ok)'
const C_AMBER = 'var(--quality)'
const C_RED = 'var(--tool)'
const C_GRAY = 'var(--ink-3)'
const C_CYAN = 'var(--coherence)'

function iterationStatus(events: NodeEvent[]): { dot: string; color: string } {
  if (events.some((e) => e.status === 'crashed')) return { dot: '✗', color: C_RED }
  if (events.some((e) => ['fail', 'semantic_fail'].includes(e.status))) return { dot: '⚠', color: C_AMBER }
  if (events.some((e) => e.status === 'degraded_input')) return { dot: '⚠', color: C_AMBER }
  if (events.some((e) => e.status === 'interrupted')) return { dot: '⏸', color: C_AMBER }
  if (events.every((e) => e.status === 'skipped')) return { dot: '⊘', color: C_GRAY }
  if (events.every((e) => e.status === 'retried')) return { dot: '↻', color: C_GRAY }
  return { dot: '✓', color: C_GREEN }
}

function iterationMetrics(events: NodeEvent[]): string {
  const dur = events.reduce((s, e) => s + (e.duration_ms ?? 0), 0)
  const cost = events.reduce((s, e) => s + (e.llm_usage?.total_cost_usd ?? 0), 0)
  const parts: string[] = []
  if (dur > 0) parts.push(`${(dur / 1000).toFixed(1)}s`)
  if (cost > 0) parts.push(`$${cost.toFixed(4)}`)
  return parts.join(' · ')
}

function findLoopAnalysis(run: RunRecord, iterations: NodeEvent[][]): LoopAnalysisResult | null {
  if (!run.loop_analyses?.length) return null
  const nodeNames = new Set(iterations[0]?.map((e) => e.node_name) ?? [])
  return run.loop_analyses.find((la) => nodeNames.has(la.node_name)) ?? null
}

export default function CycleGroup({
  iterations,
  nameCol,
  run,
  startIndex,
  onReplay,
  onReplayNode,
  replayingNode,
  nodeDiff,
  onDismissDiff,
}: {
  iterations: NodeEvent[][]
  nameCol: number
  run: RunRecord
  startIndex?: number
  onReplay?: (node: string) => void
  onReplayNode?: (node: string) => void
  replayingNode?: string | null
  nodeDiff?: NodeDiffData | null
  onDismissDiff?: () => void
}) {
  const cycleNodeNames = iterations[0]?.map((e) => e.node_name).join(' → ') ?? ''
  const loopAnalysis = findLoopAnalysis(run, iterations)
  const [collapsed, setCollapsed] = useState<Set<number>>(() => {
    const s = new Set<number>()
    iterations.forEach((events, i) => {
      if (i < iterations.length - 1 && events.every((e) => e.status === 'retried')) s.add(i)
    })
    return s
  })
  const [analysisOpen, setAnalysisOpen] = useState(false)
  let idx = startIndex ?? 0

  const toggleIter = (i: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      next.has(i) ? next.delete(i) : next.add(i)
      return next
    })
  }

  return (
    <div className="flex flex-col gap-2">
      {/* Cycle group header */}
      <div className="flex items-center gap-2 px-1">
        <span className="rounded-[4px] bg-white/[0.06] px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-text-tertiary">
          cycle
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">
          {cycleNodeNames}
        </span>
        <span className="font-mono text-[11px] font-bold text-text-tertiary">
          ×{iterations.length}
        </span>
        {/* Per-iteration status dots */}
        <span className="flex items-center gap-1 ml-1">
          {iterations.map((events, i) => {
            const s = iterationStatus(events)
            return (
              <span key={i} className="font-mono text-[10px] font-bold" style={{ color: s.color }}>
                {s.dot}
              </span>
            )
          })}
        </span>
        {/* Stall / unnecessary retry warnings */}
        {loopAnalysis?.is_stalled && (
          <span className="rounded-[4px] px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider" style={{ color: C_RED, background: 'color-mix(in srgb, var(--tool) 10%, transparent)' }}>
            stalled
          </span>
        )}
        {loopAnalysis && loopAnalysis.unnecessary_retries > 0 && (
          <span className="rounded-[4px] px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider" style={{ color: C_AMBER, background: 'color-mix(in srgb, var(--quality) 10%, transparent)' }}>
            {loopAnalysis.unnecessary_retries} unnecessary
          </span>
        )}
      </div>

      {/* LLM loop analysis panel */}
      {loopAnalysis && loopAnalysis.summary && (
        <div
          className="mx-1 rounded-[6px] border px-3 py-2"
          style={{ borderColor: 'color-mix(in srgb, var(--coherence) 20%, var(--border))', background: 'color-mix(in srgb, var(--coherence) 4%, var(--card))' }}
        >
          <div
            className="flex items-center gap-2 cursor-pointer select-none"
            onClick={() => setAnalysisOpen(!analysisOpen)}
          >
            <span className="text-[10px] font-medium uppercase tracking-wider" style={{ color: C_CYAN }}>
              loop analysis
            </span>
            <span className="font-mono text-[10px] text-text-tertiary">
              {analysisOpen ? '▾' : '▸'}
            </span>
          </div>
          {analysisOpen && (
            <div className="mt-2 flex flex-col gap-2">
              <p className="text-[12px] text-muted-foreground leading-relaxed">
                {loopAnalysis.summary}
              </p>
              {loopAnalysis.is_stalled && loopAnalysis.stall_details && (
                <div className="rounded-[4px] px-2 py-1.5 text-[11px]" style={{ color: C_RED, background: 'color-mix(in srgb, var(--tool) 8%, transparent)' }}>
                  <span className="font-medium">Stall: </span>{loopAnalysis.stall_details}
                </div>
              )}
              {loopAnalysis.unnecessary_retries > 0 && loopAnalysis.unnecessary_details && (
                <div className="rounded-[4px] px-2 py-1.5 text-[11px]" style={{ color: C_AMBER, background: 'color-mix(in srgb, var(--quality) 8%, transparent)' }}>
                  <span className="font-medium">Unnecessary retries: </span>{loopAnalysis.unnecessary_details}
                </div>
              )}
              {loopAnalysis.iteration_diffs.length > 0 && (
                <div className="flex flex-col gap-1">
                  <span className="text-[10px] font-medium uppercase tracking-wider text-text-tertiary">Changes between iterations</span>
                  {loopAnalysis.iteration_diffs.map((diff, i) => (
                    <div key={i} className="flex items-start gap-2 text-[11px] text-muted-foreground">
                      <span className="font-mono text-text-tertiary shrink-0">
                        {diff.from_attempt + 1}→{diff.to_attempt + 1}
                      </span>
                      <span>{diff.summary}</span>
                    </div>
                  ))}
                </div>
              )}
              <span className="text-[9px] text-text-tertiary">
                {loopAnalysis.model_used} · {loopAnalysis.prompt_tokens + loopAnalysis.completion_tokens} tokens · {(loopAnalysis.duration_ms / 1000).toFixed(1)}s
              </span>
            </div>
          )}
        </div>
      )}

      {iterations.map((iterEvents, iterIdx) => {
        const isRetried = iterEvents.every((e) => e.status === 'retried')
        const isCollapsed = collapsed.has(iterIdx)
        const metrics = iterationMetrics(iterEvents)

        const iterRows = isCollapsed
          ? null
          : iterEvents.map((event) => {
              const currentIdx = idx++
              return (
                <StepRow
                  key={currentIdx}
                  event={event}
                  nameCol={nameCol}
                  run={run}
                  displayIndex={currentIdx}
                  onReplay={onReplay}
                  onReplayNode={onReplayNode}
                  isReplaying={replayingNode === event.node_name}
                  nodeDiff={nodeDiff?.nodeName === event.node_name ? nodeDiff : undefined}
                  onDismissDiff={onDismissDiff}
                />
              )
            })
        if (isCollapsed) idx += iterEvents.length

        // Find diff summary for this iteration transition
        const iterDiff = loopAnalysis?.iteration_diffs.find(
          (d) => d.to_attempt === iterIdx
        )

        return (
          <div
            key={iterIdx}
            className="flex flex-col gap-2"
            style={{ opacity: isRetried ? 0.5 : 1 }}
          >
            {/* Iteration label — always shown */}
            <div
              className="flex items-center gap-2 px-1 cursor-pointer select-none"
              onClick={() => toggleIter(iterIdx)}
            >
              <span className="font-mono text-[10px] text-text-tertiary">
                {isCollapsed ? '▸' : '▾'} iteration {iterIdx + 1}
              </span>
              {metrics && (
                <span className="font-mono text-[10px] text-text-tertiary">
                  {metrics}
                </span>
              )}
              {isRetried && (
                <span className="rounded-[4px] bg-white/[0.04] px-1 py-0.5 text-[9px] font-medium uppercase tracking-wider" style={{ color: C_GRAY }}>
                  retried
                </span>
              )}
            </div>
            {/* Inline diff summary between iterations */}
            {iterDiff && (
              <div className="px-3 text-[10px] font-mono" style={{ color: C_CYAN }}>
                ↳ {iterDiff.summary}
              </div>
            )}
            {iterRows}
          </div>
        )
      })}
    </div>
  )
}
