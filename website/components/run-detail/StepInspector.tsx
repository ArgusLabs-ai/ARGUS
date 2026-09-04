'use client'

/* Node detail — a flush region under the overview, not a card. The node
   name, its status word and one lead sentence; then quiet captions with
   rows on hairlines, each carrying a 3 px rule in the colour of its
   severity. Nothing else on the region is coloured. */

import { useEffect, useState } from 'react'
import type { NodeEvent, RunRecord } from '@/lib/types'
import { formatDuration } from '@/lib/workspace'
import { stepTone, stepWord, fmtCost, fmtTokens } from '@/lib/run-detail'
import { getFailureMeta } from '@/lib/failure-labels'
import JsonGutter from './JsonGutter'
import Prose from './Prose'
import { FixPromptBody, useFixPrompt } from './FixPrompt'

function lead(step: NodeEvent): string | null {
  if (step.status === 'crashed' && step.exception) {
    const last = step.exception.split('\n').filter((l) => l.trim()).pop()
    return last ? `Raised ${last.trim()}` : 'Raised an exception.'
  }
  const insp = step.inspection
  if (insp) {
    const crit = insp.tool_failures.find((f) => f.severity === 'critical')
    if (crit) return `${getFailureMeta(crit.failure_type).label}: ${crit.evidence}`
    if (insp.is_silent_failure && insp.missing_fields.length) {
      return `Reported pass while omitting \`${insp.missing_fields.join('`, `')}\`, which downstream nodes require.`
    }
  }
  if (step.status === 'semantic_fail') return step.semantic_check?.reason ?? 'The output failed the semantic judge.'
  if (step.status === 'degraded_input' && insp) {
    const up = insp.degraded_upstream_node
    return up ? `Ran on degraded input because \`${up}\` failed to produce ${insp.degraded_fields.length ? `\`${insp.degraded_fields.join('`, `')}\`` : 'required fields'}.` : 'Ran on degraded input.'
  }
  const warn = insp?.tool_failures.find((f) => f.severity === 'warning')
  if (warn) return `${getFailureMeta(warn.failure_type).label}: ${warn.evidence}`
  return null
}

function Row({ rule, children }: { rule: string; children: React.ReactNode }) {
  return <div className="irow" style={{ ['--rule' as string]: rule }}>{children}</div>
}

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <div className="ndet-sec">
      <p className="cap"><span>{title}{count != null ? ` · ${count}` : ''}</span></p>
      {children}
    </div>
  )
}

function Json({ title, value }: { title: string; value: Record<string, unknown> | null }) {
  const [open, setOpen] = useState(false)
  if (!value || !Object.keys(value).length) return null
  const n = Object.keys(value).length
  return (
    <div className="ndet-sec">
      <p className="cap">
        <span>{title} · {n} key{n === 1 ? '' : 's'}</span>
        <a href="#" onClick={(e) => { e.preventDefault(); setOpen((v) => !v) }}>{open ? 'Hide' : 'Show'}</a>
      </p>
      {open && <div style={{ margin: '0 -34px' }}><JsonGutter value={value} maxLines={120} /></div>}
    </div>
  )
}

function NodeDetail({ step, run, onDismiss }: { step: NodeEvent; run: RunRecord; onDismiss?: () => void }) {
  const canFix = step.status !== 'pass' && step.status !== 'skipped'
  const fix = useFixPrompt(run.run_id, canFix ? step.node_name : null)
  const insp = step.inspection
  const toolFailures = insp?.tool_failures ?? []
  const semanticSignals = insp?.semantic_signals ?? []
  const missing = insp?.missing_fields ?? []
  const empties = insp?.empty_fields ?? []
  const mismatches = insp?.type_mismatches ?? []
  const anomalies = step.anomaly_signals ?? []
  const validators = step.validator_results ?? []
  const failedValidators = validators.filter((v) => !v.is_valid)
  const sc = step.semantic_check
  const signalCount = toolFailures.length + semanticSignals.length + (missing.length ? 1 : 0) + mismatches.length + failedValidators.length + anomalies.length
  const sentence = lead(step)
  const src = run.node_fn_paths?.[step.node_name]
  const tokens = step.llm_usage?.total_tokens
  const cost = step.llm_usage?.total_cost_usd
  const isRoot = run.root_cause_chain?.includes(step.node_name)

  return (
    <div className="ndet" id={`step-${step.node_name}`}>
      <div className="ndet-head">
        <span className="ndet-name">{step.node_name}</span>
        <span className={`stat ${stepTone(step.status)}`}><i />{stepWord(step.status)}</span>
        {isRoot && <span style={{ fontSize: 11, color: 'var(--tool)', letterSpacing: '.02em' }}>root cause</span>}
        <span style={{ flex: 1 }} />
        {canFix && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { void fix.load() }}>{fix.label}</button>
        )}
        {onDismiss && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onDismiss}>Close</button>
        )}
      </div>
      <p className="ndet-meta">
        <span>Step <b>{step.step_index + 1}</b></span>
        <span>· Duration <b>{formatDuration(step.duration_ms)}</b></span>
        {tokens ? <span>· Tokens <b>{fmtTokens(tokens)}</b></span> : null}
        {cost ? <span>· Cost <b>{fmtCost(cost)}</b></span> : null}
        {step.attempt_index > 0 && <span>· Attempt <b>{step.attempt_index + 1}</b></span>}
        {step.behavior_type && <span>· <b>{step.behavior_type}</b></span>}
        {src && <span>· <b>{src}</b></span>}
      </p>

      {sentence && <p className="ndet-lead"><Prose text={sentence} /></p>}
      {canFix && fix.open && (
        <FixPromptBody
          node={fix.payload?.node ?? step.node_name}
          sourcePath={fix.payload?.source_path}
          prompt={fix.payload?.prompt}
          error={fix.error}
          copied={fix.copied}
          onCopy={() => { void fix.copy() }}
          onHide={() => fix.setOpen(false)}
        />
      )}

      {signalCount > 0 && (
        <Section title="Signals" count={signalCount}>
          {missing.length > 0 && (
            <Row rule="var(--tool)">
              Missing required fields <span className="m">{missing.join(', ')}</span>
              {run.graph_edge_map?.[step.node_name]?.[0] && <div className="d">Required by <code>{run.graph_edge_map[step.node_name][0]}</code>.</div>}
            </Row>
          )}
          {mismatches.map((m, i) => (
            <Row key={`m${i}`} rule="var(--quality)">
              Type mismatch on <span className="m">{m.field_name}</span>
              <div className="d">expected <code>{m.expected_type}</code>, got <code>{m.actual_type}</code></div>
            </Row>
          ))}
          {toolFailures.map((tf, i) => {
            const meta = getFailureMeta(tf.failure_type)
            return (
              <Row key={`t${i}`} rule={tf.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'}>
                {meta.label}{tf.field_name && <> on <span className="m">{tf.field_name}</span></>}
                <div className="d">{tf.evidence}</div>
              </Row>
            )
          })}
          {semanticSignals.map((sig, i) => (
            <Row key={`s${i}`} rule="var(--semantic)">
              {sig.description} <span className="m" style={{ color: 'var(--ink-4)' }}>{sig.sig_id}</span>
              <div className="d">
                {sig.category}{sig.field_path.length ? <> · <code>{sig.field_path.join('.')}</code></> : null}
                {sig.evidence && <> · {sig.evidence}</>}
              </div>
            </Row>
          ))}
          {failedValidators.map((v, i) => (
            <Row key={`v${i}`} rule="var(--semantic)">
              Validator <span className="m">{v.validator_name}</span> failed
              {v.message && <div className="d">{v.message}</div>}
            </Row>
          ))}
          {anomalies.map((a, i) => (
            <Row key={`a${i}`} rule={a.severity === 'critical' ? 'var(--tool)' : 'var(--quality)'}>
              {a.reason} <span className="m" style={{ color: 'var(--ink-4)' }}>{a.anomaly_id} · {(a.suspicion_score * 100).toFixed(0)}%</span>
              {(a.expected_behavior || a.observed_behavior) && (
                <div className="irow-kv">
                  {a.expected_behavior && <span>Expected <b>{a.expected_behavior}</b></span>}
                  {a.observed_behavior && <span>Observed <b>{a.observed_behavior}</b></span>}
                </div>
              )}
            </Row>
          ))}
        </Section>
      )}

      {empties.length > 0 && (
        <p className="ndet-meta">Empty optional fields: <b>{empties.join(', ')}</b></p>
      )}

      {sc && (
        <Section title={`Semantic judge · ${sc.passed ? 'coherent' : 'incoherent'} · ${Math.round(sc.confidence * 100)}%`}>
          <Row rule={sc.passed ? 'var(--ok)' : 'var(--semantic)'}>
            {sc.reason}
            {(sc.evidence_considered?.length ?? 0) > 0 && (
              <div className="d">Considered: {sc.evidence_considered!.join(' · ')}</div>
            )}
            {(sc.overridden_signals?.length ?? 0) > 0 && (
              <div className="d" style={{ color: 'var(--quality)' }}>Overrode: {sc.overridden_signals!.join(' · ')}</div>
            )}
          </Row>
        </Section>
      )}

      {validators.length > failedValidators.length && (
        <p className="ndet-meta">
          Passed validators: <b>{validators.filter((v) => v.is_valid).map((v) => v.validator_name).join(', ')}</b>
        </p>
      )}

      {step.exception && (
        <Section title="Traceback">
          <div style={{ margin: '0 -34px' }}><pre className="trace">{step.exception}</pre></div>
        </Section>
      )}

      <Json title="Input state" value={step.input_state} />
      <Json title="Output" value={step.output_dict} />
    </div>
  )
}

export default function StepInspector({
  run,
  selectedNodeName,
  onDismiss,
}: {
  run: RunRecord
  selectedNodeName?: string | null
  onDismiss?: () => void
}) {
  const steps = run.steps ?? []

  useEffect(() => {
    if (!selectedNodeName) return
    const el = document.getElementById(`step-${selectedNodeName}`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [selectedNodeName])

  if (selectedNodeName) {
    const step = steps.find((s) => s.node_name === selectedNodeName)
    if (step) return <NodeDetail step={step} run={run} onDismiss={onDismiss} />
  }

  const failed = steps.find((s) => s.status !== 'pass' && s.status !== 'skipped')
  if (!failed) return null
  return <NodeDetail step={failed} run={run} />
}
