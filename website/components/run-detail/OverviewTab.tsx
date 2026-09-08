'use client'

/* Overview — one focal point. The verdict is a sentence set two sizes up
   with the culprit node the only red thing on the screen; the chain, the
   graph, the facts and the findings descend from it. No boxed regions. */

import { useEffect, useState } from 'react'
import type { RunRecord, RunSummary } from '@/lib/types'
import { useWorkspace, formatDuration } from '@/lib/workspace'
import {
  culpritNode, failureChain, headlineFinding,
  fmtCost, fmtTokens, totalCalls,
} from '@/lib/run-detail'
import Prose from './Prose'
import ExecutionGraph from './ExecutionGraph'
import FindingsPanel from './FindingsPanel'
import StepInspector from './StepInspector'
import ReplayBranches from './ReplayBranches'
import { FixPromptBody, useFixPrompt } from './FixPrompt'

type Tab = 'Overview' | 'Pipeline' | 'AI Analysis' | 'Correlations' | 'State' | 'Logs'
type FixHandle = ReturnType<typeof useFixPrompt>

function Verdict({ run, onFix, fixLabel }: { run: RunRecord; onFix?: () => void; fixLabel?: string }) {
  const steps = run.steps ?? []
  const head = headlineFinding(run)
  const who = culpritNode(run)
  const chain = failureChain(run)
  const inv = run.llm_investigation

  if (!head) {
    const reached = steps.filter((s) => s.status !== 'skipped').length
    if (run.overall_status === 'interrupted') {
      return (
        <div>
          <p className="finding">
            Paused at <span className="who" style={{ color: 'var(--quality)' }}>{run.interrupt_node ?? 'a node'}</span> awaiting
            approval. {reached} of {steps.length} steps have run.
          </p>
        </div>
      )
    }
    return (
      <div>
        <p className="finding">
          <span className="who ok">{steps.length} steps</span> passed and nothing was flagged.
          {run.duration_ms != null && <> The run took {formatDuration(run.duration_ms)}.</>}
        </p>
      </div>
    )
  }

  const surfaced = chain.length > 1 ? chain[chain.length - 1] : null
  const conf = head.confidence ?? inv?.confidence ?? null

  return (
    <div>
      <p className="finding">
        <Prose text={head.reason} who={who ?? head.node} />
        {surfaced && surfaced !== (who ?? head.node) && (
          <> The failure surfaced {chain.length > 2 ? `${chain.length - 1} nodes later` : 'downstream'} in <code>{surfaced}</code>.</>
        )}
      </p>
      <p className="finding-sub">
        <span>Root cause{typeof conf === 'number' ? ` · confidence ${conf.toFixed(2)}` : ''}</span>
        {chain.length > 0 && (
          <>
            <span className="arrow">·</span>
            {chain.map((n, i) => (
              <span key={n} style={{ display: 'contents' }}>
                {i > 0 && <span className="arrow">→</span>}
                <span className="n">{n}</span>
              </span>
            ))}
          </>
        )}
        {onFix && (
          <>
            <span className="arrow">·</span>
            <a href="#" onClick={(e) => { e.preventDefault(); onFix() }}>{fixLabel ?? 'Copy fix prompt'}</a>
          </>
        )}
      </p>
    </div>
  )
}

function Facts({ run }: { run: RunRecord }) {
  const steps = run.steps ?? []
  const reached = steps.filter((s) => s.status !== 'skipped').length
  const calls = totalCalls(run)
  return (
    <p className="facts">
      <span>Duration<b>{formatDuration(run.duration_ms)}</b></span>
      <span>Steps<b>{reached} / {steps.length}</b></span>
      {run.total_tokens != null && <span>Tokens<b>{fmtTokens(run.total_tokens)}</b></span>}
      {run.total_cost_usd != null && <span>Cost<b>{fmtCost(run.total_cost_usd)}</b></span>}
      {calls > 0 && <span>LLM calls<b>{calls}</b></span>}
      <span>Argus<b>v{run.argus_version}</b></span>
    </p>
  )
}

function Analysis({ run, onViewFull }: { run: RunRecord; onViewFull: () => void }) {
  const inv = run.llm_investigation
  if (!inv || !inv.triggered || !inv.root_cause_explanation) return null
  return (
    <div>
      <p className="cap">
        <span>AI analysis · {inv.model_used}{typeof inv.confidence === 'number' ? ` · confidence ${inv.confidence.toFixed(2)}` : ''}</span>
        <a href="#" onClick={(e) => { e.preventDefault(); onViewFull() }}>Full analysis</a>
      </p>
      <p style={{ margin: 0, fontSize: 13, lineHeight: 1.65, color: 'var(--ink-2)', maxWidth: '78ch' }}>
        {inv.root_cause_explanation}
      </p>
    </div>
  )
}

export default function OverviewTab({
  run, allRuns, onSwitchTab, fix,
}: {
  run: RunRecord
  allRuns: RunSummary[]
  onSwitchTab: (tab: Tab) => void
  fix?: FixHandle
}) {
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const { setNote } = useWorkspace()
  const findings = run.findings ?? []
  const who = culpritNode(run)
  const nodes = (run.graph_node_names ?? []).length
  const canFix = findings.some((f) => !f.suppressed) || run.overall_status !== 'clean'

  useEffect(() => {
    if (fix?.open) document.querySelector('.fix-panel')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [fix?.open, fix?.payload])

  /* The type-annotation hint lives in the workspace top bar, as in the spec. */
  useEffect(() => {
    const steps = run.steps ?? []
    const un = steps.filter((s) => (s.inspection?.unannotated_successors?.length ?? 0) > 0).length
    if (steps.length && un / steps.length >= 0.5) {
      setNote({ key: `unannotated:${run.run_id}`, text: `${un} node${un === 1 ? '' : 's'} lack type annotations` })
    } else {
      setNote(null)
    }
    return () => setNote(null)
  }, [run, setNote])

  return (
    <div className="wc">
      <Verdict
        run={run}
        onFix={canFix && fix ? () => { void fix.copy() } : undefined}
        fixLabel={fix?.label}
      />

      {canFix && fix && (fix.open || (fix.busy && !fix.payload)) && (
        <FixPromptBody
          node={fix?.payload?.node ?? who}
          sourcePath={fix?.payload?.source_path}
          prompt={fix?.payload?.prompt}
          error={fix?.error}
          copied={fix?.copied}
          busy={fix?.busy}
          onCopy={fix ? () => { void fix.copy() } : undefined}
          onHide={fix ? () => fix.setOpen(false) : undefined}
        />
      )}

      {nodes > 0 && (
        <div>
          <p className="cap">
            <span>Execution graph · {nodes} nodes</span>
            <a href="#" onClick={(e) => { e.preventDefault(); onSwitchTab('Pipeline') }}>Expand</a>
          </p>
          <ExecutionGraph run={run} flush selectedNode={selectedNode} onSelectNode={setSelectedNode} />
        </div>
      )}

      <Facts run={run} />

      <FindingsPanel
        findings={findings}
        runId={run.run_id}
        culprit={who}
        onSelectNode={setSelectedNode}
      />

      <Analysis run={run} onViewFull={() => onSwitchTab('AI Analysis')} />

      <div id="step-inspector">
        <StepInspector run={run} selectedNodeName={selectedNode} onDismiss={() => setSelectedNode(null)} />
      </div>

      <ReplayBranches run={run} allRuns={allRuns} onSwitchTab={onSwitchTab} />
    </div>
  )
}
