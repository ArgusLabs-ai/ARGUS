'use client'

/* Behaviour profile per node: a caption and label/value rows. */

import type { RunRecord } from '@/lib/types'

const BEHAVIOR_LABELS: Record<string, string> = {
  structured_json: 'structured JSON',
  retrieval_result: 'retrieval result',
  classification: 'classification',
  detailed_text: 'detailed text',
  tool_output: 'tool output',
  reasoning_chain: 'reasoning chain',
  chat_response: 'chat response',
  code_generation: 'code generation',
}

export default function BehaviorPanel({ run }: { run: RunRecord }) {
  const steps = (run.steps ?? []).filter((s) => s.behavior_type)
  if (!steps.length) return null
  const cfg = run.behavior_config
  const total = steps.reduce((n, s) => n + (s.anomaly_signals?.length ?? 0), 0)

  return (
    <div>
      <p className="cap">
        <span>
          Behaviour profile · {steps.length} nodes
          {cfg?.default_behavior_type && <> · default {BEHAVIOR_LABELS[cfg.default_behavior_type] ?? cfg.default_behavior_type}</>}
          {total > 0 && <> · {total} anomal{total === 1 ? 'y' : 'ies'}</>}
        </span>
      </p>
      <div style={{ maxWidth: 620 }}>
        {steps.map((s) => {
          const kind = cfg?.node_behaviors?.[s.node_name] ? 'override' : cfg?.default_behavior_type === s.behavior_type ? 'pipeline' : 'inferred'
          const n = s.anomaly_signals?.length ?? 0
          const peak = Math.max(0, ...(s.anomaly_signals?.map((a) => a.suspicion_score) ?? []))
          return (
            <div key={`${s.node_name}-${s.step_index}`} className="crow">
              <span style={{ fontFamily: 'var(--mono)', color: 'var(--ink-2)' }}>{s.node_name}</span>
              <b style={{ fontFamily: 'var(--sans)', fontWeight: 400, color: 'var(--ink-2)' }}>
                {BEHAVIOR_LABELS[s.behavior_type!] ?? s.behavior_type}
                <span style={{ color: 'var(--ink-4)' }}> · {kind}</span>
                {n > 0 && <span style={{ fontFamily: 'var(--mono)', color: peak > 0.7 ? 'var(--tool)' : 'var(--quality)' }}> · {n} · {(peak * 100).toFixed(0)}%</span>}
              </b>
            </div>
          )
        })}
      </div>
    </div>
  )
}
