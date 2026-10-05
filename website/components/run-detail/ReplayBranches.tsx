'use client'

/* Rerun branches: a quiet caption and rows on hairlines, nested one step
   per generation. Each row carries its run's status dot. */

import { useState, useEffect } from 'react'
import type { RunRecord, RunSummary } from '@/lib/types'
import { useWorkspace, toneFor, statusWord, formatDuration, shortRunId, relativeAge } from '@/lib/workspace'

type Tab = 'Overview' | 'Pipeline' | 'AI Analysis' | 'Correlations' | 'State' | 'Logs'

interface ReplayTreeNode {
  run_id: string
  started_at: string
  overall_status: string
  duration_ms: number | null
  step_count: number
  replay_from_step: string | null
  children: ReplayTreeNode[]
}

const DOT: Record<string, string> = {
  ok: 'var(--ok)', bad: 'var(--tool)', warn: 'var(--quality)', sem: 'var(--semantic)', live: 'var(--iris)', mute: 'var(--ink-4)',
}

function Branch({ node, depth, open }: { node: ReplayTreeNode; depth: number; open: (id: string) => void }) {
  const tone = toneFor(node.overall_status)
  return (
    <>
      <div
        className="branch"
        role="button"
        tabIndex={0}
        style={{ paddingLeft: 34 + depth * 18 }}
        onClick={() => open(node.run_id)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(node.run_id) } }}
      >
        <i style={{ background: DOT[tone] }} />
        <span>
          <span className="lb">{shortRunId(node.run_id)}</span>
          <span className="d">
            {node.replay_from_step ? `replay from ${node.replay_from_step}` : 'replay'} · {statusWord(node.overall_status)}
          </span>
        </span>
        <span className="r">{node.step_count} steps · {formatDuration(node.duration_ms)} · {relativeAge(node.started_at)}</span>
      </div>
      {node.children?.map((c) => <Branch key={c.run_id} node={c} depth={depth + 1} open={open} />)}
    </>
  )
}

export default function ReplayBranches({
  run,
  allRuns,
  onSwitchTab,
}: {
  run: RunRecord
  allRuns: RunSummary[]
  onSwitchTab: (tab: Tab) => void
}) {
  const { openRun } = useWorkspace()
  const [children, setChildren] = useState<ReplayTreeNode[]>([])

  useEffect(() => {
    fetch(`/api/runs/${run.run_id}/tree`, { cache: 'no-store' })
      .then((r) => r.ok ? r.json() : Promise.reject())
      .then((tree: ReplayTreeNode) => setChildren(tree.children ?? []))
      .catch(() => {
        setChildren(
          allRuns
            .filter((r) => r.parent_run_id === run.run_id)
            .map((r) => ({
              run_id: r.run_id,
              started_at: r.started_at,
              overall_status: r.overall_status,
              duration_ms: r.duration_ms,
              step_count: r.step_count,
              replay_from_step: r.replay_from_step ?? null,
              children: [],
            })),
        )
      })
  }, [run.run_id, allRuns])

  const count = (nodes: ReplayTreeNode[]): number => nodes.reduce((n, c) => n + 1 + count(c.children ?? []), 0)
  const total = count(children)
  if (total === 0 && !run.parent_run_id) return null

  return (
    <section className="ov-sec">
      <div className="sh">
        <h3>Reruns</h3>
        <span className="sh-n">{total}</span>
        {run.parent_run_id && <span className="chip chip-iris chip-mono">replay of {shortRunId(run.parent_run_id)}</span>}
        <span className="sh-sp" />
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => onSwitchTab('Pipeline')}>Rerun from a step</button>
      </div>
      {total > 0 ? (
        <div className="blist">
          {children.map((c) => <Branch key={c.run_id} node={c} depth={0} open={(id) => openRun(id)} />)}
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: 12.5, color: 'var(--ink-4)' }}>No reruns yet. Open the Pipeline tab and rerun from any step.</p>
      )}
    </section>
  )
}
