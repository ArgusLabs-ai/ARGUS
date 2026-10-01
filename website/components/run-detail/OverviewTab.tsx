'use client'

/* Overview — compartments, not boxes. Root cause first (verdict sentence,
   category capsule, the blame path as node capsules, the fix prompt one
   click away), then the run's numbers, the graph, findings grouped by node
   and the AI read. Each region opens on a hairline and a heading; colour is
   reserved for signal. Detail waits for a click: step detail opens from a
   finding or a graph node, long prose is clamped. */

import { useEffect, useMemo, useState } from 'react'
import { Wand2, ChevronRight } from 'lucide-react'
import type { RunRecord, RunSummary } from '@/lib/types'
import { useWorkspace, formatDuration } from '@/lib/workspace'
import { displayNodes, displayTopology } from '@/lib/run-utils'
import {
  activeFindings, culpritNode, failureChain, headlineFinding,
  fmtCost, fmtTokens, totalCalls,
} from '@/lib/run-detail'
import { findingMeta } from '@/lib/failure-labels'
import { pathBetween } from '@/lib/graph-model'
import Prose from './Prose'
import ExecutionGraph from './ExecutionGraph'
import FindingsPanel from './FindingsPanel'
import StepInspector from './StepInspector'
import ReplayBranches from './ReplayBranches'
import { FixPromptBody, useFixPrompt } from './FixPrompt'

type Tab = 'Overview' | 'Pipeline' | 'AI Analysis' | 'Correlations' | 'State' | 'Logs'
type FixHandle = ReturnType<typeof useFixPrompt>

/* Origin → every hop → where it surfaced. */
function blamePath(run: RunRecord): string[] {
  const chain = failureChain(run)
  if (chain.length < 2) return chain
  const edges = displayTopology(run.graph_node_names, run.graph_edge_map).edges
  const out = [chain[0]]
  for (let i = 1; i < chain.length; i++) out.push(...pathBetween(edges, chain[i - 1], chain[i]).slice(1))
  return out
}

function FixRow({ fix, node }: { fix: FixHandle; node: string | null }) {
  const p = fix.payload
  return (
    <div className="fixrow-wrap">
      <div className="fixrow">
        <Wand2 className="fixrow-ico" />
        <span className="fixrow-t">
          Fix prompt for <code>{p?.node ?? node ?? 'root cause'}</code>
          {p?.source_path && <span className="fixrow-m">{p.source_path}</span>}
        </span>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => (fix.open ? fix.setOpen(false) : void fix.load())}>
          {fix.open ? 'Hide' : 'View'}
        </button>
        <button type="button" className="btn btn-sm" onClick={() => { void fix.copy() }}>{fix.label}</button>
      </div>
      {(fix.open || fix.error) && (
        <FixPromptBody
          node={p?.node ?? node}
          sourcePath={p?.source_path}
          prompt={p?.prompt}
          error={fix.error}
          copied={fix.copied}
          busy={fix.busy}
          onCopy={() => { void fix.copy() }}
          onHide={() => fix.setOpen(false)}
          sanitized={fix.sanitized}
          onToggleValues={() => { void fix.toggleValues() }}
        />
      )}
    </div>
  )
}

function Verdict({ run, fix, canFix }: { run: RunRecord; fix?: FixHandle; canFix: boolean }) {
  const steps = run.steps ?? []
  const head = headlineFinding(run)
  const who = culpritNode(run)
  const path = useMemo(() => blamePath(run), [run])
  const inv = run.llm_investigation

  if (!head) {
    const reached = steps.filter((s) => s.status !== 'skipped').length
    const paused = run.overall_status === 'interrupted'
    return (
      <section className="ov-hero">
        <div className="ov-eyebrow">
          <span className={`eyebrow ${paused ? 'warn' : 'ok'}`}>{paused ? 'Paused' : 'Verdict'}</span>
          <span className={`chip ${paused ? 'chip-run' : 'chip-ok'}`}><span className="dot" />{paused ? 'awaiting approval' : 'clean'}</span>
        </div>
        <p className="finding">
          {paused ? (
            <>Paused at <span className="who" style={{ color: 'var(--quality)' }}>{run.interrupt_node ?? 'a node'}</span> awaiting approval. {reached} of {steps.length} steps have run.</>
          ) : (
            <><span className="who ok">{steps.length} steps</span> passed and nothing was flagged.{run.duration_ms != null && <> The run took {formatDuration(run.duration_ms)}.</>}</>
          )}
        </p>
      </section>
    )
  }

  const fm = findingMeta(head)
  /* A clean run can still carry advisory warnings — nothing to blame. */
  const advisory = run.overall_status === 'clean'
  const conf = head.confidence ?? inv?.confidence ?? null
  const crashed = new Set(steps.filter((s) => s.status === 'crashed').map((s) => s.node_name))

  return (
    <section className="ov-hero">
      <div className="ov-eyebrow">
        <span className={`eyebrow ${advisory ? 'warn' : 'bad'}`}>{advisory ? 'Advisory' : 'Root cause'}</span>
        {advisory && <span className="chip chip-ok"><span className="dot" />clean</span>}
        <span className={`chip ${fm.chip}`}><span className="dot" />{fm.category} · {fm.label}</span>
      </div>
      <p className="finding" title={typeof conf === 'number' ? `confidence ${conf.toFixed(2)}` : undefined}>
        <Prose text={head.reason} who={advisory ? null : who ?? head.node} />
      </p>
      {path.length > 1 && (
        <div className="ov-path">
          <span className="ov-path-l">Blame path</span>
          <div className="rc-chain">
            {path.map((n, i) => (
              <span key={n} style={{ display: 'contents' }}>
                {i > 0 && <ChevronRight className="rc-arrow-i" />}
                <span className={`rc-node${i === 0 ? ' culprit' : ''}${crashed.has(n) ? ' crashed' : ''}`}>
                  {n}
                  {i === 0 && <em>origin</em>}
                  {crashed.has(n) && <em>raised</em>}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
      {canFix && fix && <FixRow fix={fix} node={who} />}
    </section>
  )
}

function Stats({ run }: { run: RunRecord }) {
  const steps = run.steps ?? []
  const reached = steps.filter((s) => s.status !== 'skipped').length
  const calls = totalCalls(run)
  const active = activeFindings(run)
  const crit = active.filter((f) => f.severity === 'critical').length
  const warn = active.filter((f) => f.severity === 'warning').length
  return (
    <dl className="ov-stats">
      <div><dt>Duration</dt><dd>{formatDuration(run.duration_ms)}</dd></div>
      <div><dt>Steps</dt><dd>{reached}<small> / {steps.length}</small></dd></div>
      <div>
        <dt>Findings</dt>
        <dd>
          {active.length}
          {crit > 0 && <span className="ov-sev bad">{crit} critical</span>}
          {warn > 0 && <span className="ov-sev warn">{warn} warn</span>}
        </dd>
      </div>
      {!!run.total_tokens && <div><dt>Tokens</dt><dd>{fmtTokens(run.total_tokens)}</dd></div>}
      {!!run.total_cost_usd && <div><dt>Cost</dt><dd>{fmtCost(run.total_cost_usd)}</dd></div>}
      {calls > 0 && <div><dt>LLM calls</dt><dd>{calls}</dd></div>}
    </dl>
  )
}

function Analysis({ run, onViewFull }: { run: RunRecord; onViewFull: () => void }) {
  const inv = run.llm_investigation
  if (!inv || !inv.triggered || !inv.root_cause_explanation) return null
  const pct = typeof inv.confidence === 'number' ? Math.round(inv.confidence * 100) : null
  return (
    <section className="ov-sec">
      <div className="sh">
        <h3>AI analysis</h3>
        {inv.model_used && <span className="chip chip-iris chip-mono">{inv.model_used}</span>}
        {pct != null && (
          <span className="sh-conf">
            <span className="meter"><i style={{ width: `${pct}%`, background: 'var(--iris)' }} /></span>
            {pct}%
          </span>
        )}
        <span className="sh-sp" />
        <button type="button" className="btn btn-sm btn-ghost" onClick={onViewFull}>Full analysis<ChevronRight /></button>
      </div>
      <p className="ov-prose clamp">{inv.root_cause_explanation}</p>
    </section>
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
  const nodes = displayNodes(run.graph_node_names).length
  const canFix = (run.root_cause_chain?.length ?? 0) > 0 || !!run.first_failure_step

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
    <div className="wc ov">
      <Verdict run={run} fix={fix} canFix={canFix} />

      <Stats run={run} />

      {nodes > 0 && (
        <ExecutionGraph
          run={run}
          flush
          selectedNode={selectedNode}
          onSelectNode={setSelectedNode}
          onViewFull={() => onSwitchTab('Pipeline')}
        />
      )}

      <FindingsPanel
        findings={findings}
        run={run}
        culprit={who}
        onSelectNode={setSelectedNode}
      />

      <Analysis run={run} onViewFull={() => onSwitchTab('AI Analysis')} />

      {selectedNode && (
        <div id="step-inspector">
          <StepInspector run={run} selectedNodeName={selectedNode} onDismiss={() => setSelectedNode(null)} />
        </div>
      )}

      <ReplayBranches run={run} allRuns={allRuns} onSwitchTab={onSwitchTab} />
    </div>
  )
}
