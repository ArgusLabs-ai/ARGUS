'use client'

/* Pipeline — the execution tree, then steps as rows on hairlines. Every
   row has identical rhythm; the ones that failed are marked by a 3 px rule
   at the left edge and a coloured status word. A row expands in place to
   show its input, output and traceback in the editor idiom. */

import { Fragment, useMemo, useState, type ReactNode } from 'react'
import type { NodeEvent, RunRecord } from '@/lib/types'
import { formatDuration } from '@/lib/workspace'
import { stepFlag, stepNote, stepTone, stepWord } from '@/lib/run-detail'
import { SENTINEL_NODES } from '@/lib/run-utils'
import { segmentEvents } from '@/lib/topology'
import ReplayControls, { type NodeDiffData } from './ReplayControls'
import JsonGutter from './JsonGutter'
import Prose from './Prose'
import FixPromptButton from './FixPrompt'

/* ── execution tree ─────────────────────────────────────────────── */

function statusColor(run: RunRecord, node: string): string | undefined {
  const st = (run.steps ?? []).find((s) => s.node_name === node)?.status
  switch (st) {
    case 'crashed': case 'fail': return 'var(--tool)'
    case 'semantic_fail': return 'var(--semantic)'
    case 'degraded_input': case 'interrupted': return 'var(--quality)'
    case 'skipped': case undefined: return 'var(--ink-4)'
    default: return undefined
  }
}

function Tree({ run }: { run: RunRecord }) {
  const lines = useMemo(() => {
    const names = run.graph_node_names ?? []
    const edges = run.graph_edge_map ?? {}
    const indeg = new Map<string, number>(names.map((n) => [n, 0]))
    for (const tos of Object.values(edges)) for (const t of tos) if (indeg.has(t)) indeg.set(t, (indeg.get(t) ?? 0) + 1)
    let roots = names.filter((n) => (indeg.get(n) ?? 0) === 0)
    if (!roots.length) roots = names.slice(0, 1)

    const out: { prefix: string; node: string }[] = []
    const seen = new Set<string>()
    const walk = (node: string, prefix: string, isLast: boolean, depth: number) => {
      out.push({ prefix: depth === 0 ? '' : `${prefix}${isLast ? '└─ ' : '├─ '}`, node })
      if (seen.has(node)) return
      seen.add(node)
      const kids = (edges[node] ?? []).filter((k) => names.includes(k))
      const nextPrefix = depth === 0 ? '  ' : `${prefix}${isLast ? '     ' : '│    '}`
      kids.forEach((k, i) => walk(k, nextPrefix, i === kids.length - 1, depth + 1))
    }
    roots.forEach((r, i) => walk(r, '', i === roots.length - 1, 0))
    const missing = names.filter((n) => !seen.has(n))
    missing.forEach((n) => out.push({ prefix: '', node: n }))
    return out
  }, [run])

  if (!lines.length) return null
  return (
    <div>
      <p className="cap"><span>Execution tree</span></p>
      <pre style={{ margin: 0, fontFamily: 'var(--mono)', fontSize: 12.5, lineHeight: 1.85, color: 'var(--ink-2)', overflowX: 'auto' }}>
        {lines.map((l, i) => (
          <Fragment key={i}>
            <span style={{ color: 'var(--ink-4)' }}>{l.prefix}</span>
            <span style={{ color: statusColor(run, l.node) }}>{l.node}</span>
            {'\n'}
          </Fragment>
        ))}
      </pre>
    </div>
  )
}

/* ── step rows ──────────────────────────────────────────────────── */

function Expanded({ step, diff, onDismissDiff }: { step: NodeEvent; diff?: NodeDiffData; onDismissDiff?: () => void }) {
  return (
    <div className="srow-x" onClick={(e) => e.stopPropagation()}>
      {diff && (
        <>
          <p className="cap">
            <span>
              Node rerun · {diff.originalStep.status} → {diff.replayStep.status}
              {diff.originalStep.status !== diff.replayStep.status && (
                <span style={{ color: diff.replayStep.status === 'pass' ? 'var(--ok)' : 'var(--tool)' }}>
                  {' '}· {diff.replayStep.status === 'pass' ? 'fixed' : 'changed'}
                </span>
              )}
            </span>
            {onDismissDiff && <a href="#" onClick={(e) => { e.preventDefault(); onDismissDiff() }}>Dismiss</a>}
          </p>
          {diff.aiSummary && (
            <p style={{ margin: '8px 34px 0', fontSize: 12.5, color: 'var(--ink-2)', lineHeight: 1.6, maxWidth: '78ch' }}>{diff.aiSummary}</p>
          )}
          <p className="cap"><span>Output before</span></p>
          <JsonGutter value={diff.originalStep.output_dict} maxLines={60} emptyText="no output" />
          <p className="cap"><span>Output after rerun</span></p>
          <JsonGutter value={diff.replayStep.output_dict} maxLines={60} emptyText="no output" />
        </>
      )}
      {step.exception && (
        <>
          <p className="cap"><span>Traceback</span></p>
          <pre className="trace" style={{ marginTop: 8 }}>{step.exception}</pre>
        </>
      )}
      <p className="cap"><span>Input state</span></p>
      <JsonGutter value={step.input_state} maxLines={80} emptyText="no input captured" />
      <p className="cap"><span>Output</span></p>
      <JsonGutter value={step.output_dict} maxLines={80} emptyText="no output" />
    </div>
  )
}

function Row({
  step, index, run, iter, onReplay, onReplayNode, replaying, diff, onDismissDiff,
}: {
  step: NodeEvent
  index: number
  run: RunRecord
  iter?: boolean
  onReplay: (n: string) => void
  onReplayNode: (n: string) => void
  replaying: boolean
  diff?: NodeDiffData
  onDismissDiff?: () => void
}) {
  const [open, setOpen] = useState(!!diff)
  const note = stepNote(step, run)
  const flag = stepFlag(step.status)
  const isRoot = run.root_cause_chain?.includes(step.node_name)
  const canAct = !SENTINEL_NODES.has(step.node_name) && step.status !== 'skipped'
  const src = run.node_fn_paths?.[step.node_name]
  const expanded = open || !!diff

  return (
    <div
      className={`srow${flag ? ` ${flag}` : ''}${expanded ? ' on' : ''}${iter ? ' iter' : ''}`}
      onClick={() => setOpen((v) => !v)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen((v) => !v) } }}
    >
      <span className="srow-n">{String(index).padStart(2, '0')}</span>
      <div style={{ minWidth: 0 }}>
        <div className="srow-nm">
          {step.node_name}
          {isRoot && <span className="rc">root cause</span>}
          {src && <span className="src">{src}</span>}
        </div>
        {note && <div className="srow-d"><Prose text={note} /></div>}
        {replaying && <div className="srow-d" style={{ color: 'var(--iris-bright)' }}>Rerunning this node…</div>}
      </div>
      <div className="srow-r">
        {canAct && !replaying && (
          <span className="srow-act">
            {flag && <FixPromptButton runId={run.run_id} node={step.node_name} className="btn btn-ghost" showPanel={false} />}
            <button type="button" className="btn btn-ghost" onClick={(e) => { e.stopPropagation(); onReplayNode(step.node_name) }}>Rerun node</button>
            <button type="button" className="btn btn-ghost" onClick={(e) => { e.stopPropagation(); onReplay(step.node_name) }}>Rerun from here</button>
          </span>
        )}
        <span className={`stat ${stepTone(step.status)}`}><i />{stepWord(step.status)}</span>
        <span className="srow-t">{step.status === 'skipped' ? '—' : formatDuration(step.duration_ms)}</span>
      </div>
      {expanded && <Expanded step={step} diff={diff} onDismissDiff={onDismissDiff} />}
    </div>
  )
}

function Marker({ children }: { children: ReactNode }) {
  return <div className="srow group">{children}</div>
}

export default function PipelineTab({ run }: { run: RunRecord }) {
  const steps = run.steps ?? []
  const segments = useMemo(() => segmentEvents(steps, run.graph_edge_map), [steps, run.graph_edge_map])
  const reached = steps.filter((s) => s.status !== 'skipped').length

  return (
    <div className="wc">
    <ReplayControls runId={run.run_id} run={run}>
      {(handleReplay, handleReplayNode, { replayingNode, nodeDiff, dismissDiff }) => {
        let n = 0
        const row = (s: NodeEvent, iter?: boolean) => {
          n += 1
          return (
            <Row
              key={`${s.node_name}-${s.step_index}-${s.attempt_index}`}
              step={s}
              index={n}
              run={run}
              iter={iter}
              onReplay={handleReplay}
              onReplayNode={handleReplayNode}
              replaying={replayingNode === s.node_name}
              diff={nodeDiff?.nodeName === s.node_name ? nodeDiff : undefined}
              onDismissDiff={dismissDiff}
            />
          )
        }
        return (
          <>
            <Tree run={run} />
            <div>
              <p className="cap"><span>Steps · {reached} of {steps.length} reached · Rerun node / Rerun from here on a failed row</span></p>
              <div className="slist">
                {segments.map((seg, si) => {
                  if (seg.type === 'normal') return <Fragment key={si}>{seg.events.map((e) => row(e))}</Fragment>
                  if (seg.type === 'parallel') {
                    return (
                      <Fragment key={si}>
                        <Marker>parallel · {seg.events.length} branches</Marker>
                        {seg.events.map((e) => row(e))}
                      </Fragment>
                    )
                  }
                  return (
                    <Fragment key={si}>
                      {seg.iterations.map((iter, ii) => (
                        <Fragment key={ii}>
                          <Marker>loop · iteration {ii + 1} of {seg.iterations.length}</Marker>
                          {iter.map((e) => row(e, true))}
                        </Fragment>
                      ))}
                    </Fragment>
                  )
                })}
                {steps.length === 0 && <Marker>No steps were recorded.</Marker>}
              </div>
            </div>
          </>
        )
      }}
    </ReplayControls>
    </div>
  )
}
