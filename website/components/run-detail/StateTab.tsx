'use client'

/* State — the run's accumulated state as a code editor: line-number
   gutter, syntax colour, flush ground. The one addition is a lint-style
   flag on the lines that matter: fields a node dropped or left empty. */

import { useMemo } from 'react'
import type { RunRecord } from '@/lib/types'
import JsonGutter, { type LintFlag } from './JsonGutter'
import BehaviorPanel from './BehaviorPanel'

function accumulate(run: RunRecord): { state: Record<string, unknown>; flags: LintFlag[] } {
  const state: Record<string, unknown> = {
    run_id: run.run_id,
    status: run.overall_status,
    initial_state: run.initial_state ?? {},
  }
  const flags: LintFlag[] = []
  const requiredBy = new Map<string, string>()

  for (const step of run.steps ?? []) {
    if (step.status === 'skipped') continue
    const out = step.output_dict ?? {}
    const insp = step.inspection
    const succ = run.graph_edge_map?.[step.node_name]?.[0]
    const node: Record<string, unknown> = { ...out }

    for (const f of insp?.missing_fields ?? []) {
      if (!(f in node)) node[f] = null
      flags.push({ path: `${step.node_name}.${f}`, note: `dropped${succ ? `, required by ${succ}` : ''}`, tone: 'bad' })
      if (succ) requiredBy.set(f, succ)
    }
    for (const f of insp?.empty_fields ?? []) {
      flags.push({ path: `${step.node_name}.${f}`, note: `empty${succ ? `, ${succ} may degrade` : ''}`, tone: 'warn' })
    }
    for (const m of insp?.type_mismatches ?? []) {
      flags.push({ path: `${step.node_name}.${m.field_name}`, note: `expected ${m.expected_type}, got ${m.actual_type}`, tone: 'warn' })
    }
    for (const tf of insp?.tool_failures ?? []) {
      if (tf.field_name && tf.field_name in node && !flags.some((x) => x.path === `${step.node_name}.${tf.field_name}`)) {
        flags.push({ path: `${step.node_name}.${tf.field_name}`, note: tf.failure_type.replace(/_/g, ' '), tone: tf.severity === 'critical' ? 'bad' : 'warn' })
      }
    }
    if (step.status === 'crashed') {
      const first = step.exception?.split('\n').filter((l) => l.trim()).pop()?.trim()
      node._crashed = first ?? true
      flags.push({ path: `${step.node_name}._crashed`, note: 'raised here', tone: 'bad' })
    }
    if (Object.keys(node).length === 0 && step.status === 'fail') {
      flags.push({ path: step.node_name, note: 'empty state update', tone: 'bad' })
    }
    state[step.node_name] = node
  }

  if (run.state_patch && Object.keys(run.state_patch).length) state.state_patch = run.state_patch
  state.interrupted = run.interrupted
  return { state, flags }
}

export default function StateTab({ run }: { run: RunRecord }) {
  const { state, flags } = useMemo(() => accumulate(run), [run])
  return (
    <div className="wc" style={{ paddingTop: 22 }}>
      <div>
        <p className="cap">
          <span>Accumulated state · {Object.keys(state).length} keys{flags.length > 0 && <> · <span style={{ color: 'var(--tool)' }}>{flags.length} flagged</span></>}</span>
          <a
            href="#"
            onClick={(e) => { e.preventDefault(); navigator.clipboard?.writeText(JSON.stringify(state, null, 2)).catch(() => {}) }}
          >
            Copy JSON
          </a>
        </p>
        <JsonGutter value={state} flags={flags} />
      </div>
      <BehaviorPanel run={run} />
    </div>
  )
}
