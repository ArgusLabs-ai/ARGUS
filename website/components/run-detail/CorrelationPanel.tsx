'use client'

import type { RunRecord, ToolChainFinding } from '@/lib/types'

const FINDING_LABELS: Record<string, string> = {
  retry_storm: 'retry storm',
  ordering_anomaly: 'ordering',
  unused_result: 'unused result',
  argument_degradation: 'arg degradation',
}

export default function CorrelationPanel({ run }: { run: RunRecord }) {
  const corr = run.correlation
  const tcFindings = run.tool_chain_findings ?? []
  const hasCorrelation = corr && (corr.degradation_origins.length > 0 || corr.propagation_chains.length > 0)
  const hasToolChain = tcFindings.length > 0

  if (!hasCorrelation && !hasToolChain) return null

  const primary = corr?.degradation_origins[0] ?? null
  const confColor = primary
    ? primary.confidence >= 0.8 ? 'var(--failure)' : primary.confidence >= 0.5 ? 'var(--warning)' : 'var(--muted-foreground)'
    : 'var(--muted-foreground)'

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      {/* Correlation section */}
      {hasCorrelation && corr && (
        <>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-[11px] font-medium uppercase tracking-wider" style={{ color: 'var(--text-tertiary)' }}>
              Correlation
            </span>
            {primary && (
              <span
                className="ml-auto text-[11px] font-medium px-2.5 py-0.5 rounded-full"
                style={{
                  color: confColor,
                  backgroundColor: `color-mix(in srgb, ${confColor} 10%, transparent)`,
                  border: `1px solid color-mix(in srgb, ${confColor} 40%, transparent)`,
                }}
              >
                {(primary.confidence * 100).toFixed(0)}% confidence
              </span>
            )}
          </div>

          <div className="mt-4 rounded-[8px] border border-border bg-background p-4">
            <div className="space-y-5">
              {/* Origin */}
              {primary && (
                <div>
                  <div className="text-[11px] font-medium uppercase tracking-wider mb-2" style={{ color: 'var(--text-tertiary)' }}>
                    Origin
                  </div>
                  <div className="flex items-center gap-3 flex-wrap">
                    <span
                      className="font-mono text-[13px] font-bold px-2.5 py-1 rounded-lg"
                      style={{ color: confColor, background: 'var(--card)' }}
                    >
                      {primary.node_name}
                    </span>
                    <span className="text-[12px] text-muted-foreground">step {primary.step_index}</span>
                  </div>
                  {primary.signal_types.length > 0 && (
                    <div className="mt-2 flex items-center gap-1.5 flex-wrap">
                      {primary.signal_types.map((sig, i) => (
                        <span
                          key={i}
                          className="text-[10px] px-2 py-0.5 rounded-full"
                          style={{ background: 'var(--hover)', color: 'var(--muted-foreground)' }}
                        >
                          {sig}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Propagation chains */}
              {corr.propagation_chains.length > 0 && (
                <div>
                  <div className="text-[11px] font-medium uppercase tracking-wider mb-2" style={{ color: 'var(--text-tertiary)' }}>
                    Propagation
                  </div>
                  <div className="space-y-3">
                    {corr.propagation_chains.slice(0, 2).map((chain, i) => (
                      <div key={i}>
                        <div className="flex items-center gap-1.5 flex-wrap">
                          {chain.nodes.map((node, ni) => (
                            <span key={ni} className="flex items-center gap-1.5">
                              <span
                                className="font-mono text-[12px] font-semibold px-2 py-0.5 rounded text-foreground"
                                style={{ background: 'var(--hover)' }}
                              >
                                {node}
                              </span>
                              {ni < chain.nodes.length - 1 && (
                                <span className="text-[10px] text-muted-foreground/50">→</span>
                              )}
                            </span>
                          ))}
                        </div>
                        <span
                          className="inline-block mt-1.5 text-[10px] px-2 py-0.5 rounded-full text-muted-foreground"
                          style={{ background: 'var(--card)' }}
                        >
                          {chain.chain_type}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Causal summary */}
              <div>
                <div className="text-[11px] font-medium uppercase tracking-wider mb-2" style={{ color: 'var(--text-tertiary)' }}>
                  Summary
                </div>
                <p className="mt-1.5 border-l-2 border-border pl-3 text-sm italic leading-relaxed" style={{ color: 'var(--ink-2)' }}>
                  &ldquo;{corr.causal_summary.split(/(`[^`]+`)/).map((part, i) =>
                    part.startsWith('`') && part.endsWith('`')
                      ? <code key={i} className="font-mono not-italic text-foreground">{part.slice(1, -1)}</code>
                      : part
                  )}&rdquo;
                </p>
              </div>

              {/* Replay impact */}
              {corr.replay_impact && (
                <div>
                  <div className="text-[11px] font-medium uppercase tracking-wider mb-2" style={{ color: 'var(--text-tertiary)' }}>
                    Replay Impact
                  </div>
                  <div className="flex items-center gap-2 flex-wrap">
                    {corr.replay_impact.improved_nodes.length > 0 && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-[11px] text-muted-foreground">improved:</span>
                        {corr.replay_impact.improved_nodes.map((n, i) => (
                          <span
                            key={i}
                            className="font-mono text-[11px] px-2 py-0.5 rounded-full"
                            style={{ background: 'var(--ok-dim)', color: 'var(--success)', border: '1px solid color-mix(in srgb, var(--ok) 34%, transparent)' }}
                          >
                            {n}
                          </span>
                        ))}
                      </div>
                    )}
                    {corr.replay_impact.regressed_nodes.length > 0 && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-[11px] text-muted-foreground">regressed:</span>
                        {corr.replay_impact.regressed_nodes.map((n, i) => (
                          <span
                            key={i}
                            className="font-mono text-[11px] px-2 py-0.5 rounded-full"
                            style={{ background: 'var(--tool-dim)', color: 'var(--failure)', border: '1px solid color-mix(in srgb, var(--tool) 34%, transparent)' }}
                          >
                            {n}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  {corr.replay_impact.summary && (
                    <p className="mt-2 text-[12px] italic text-muted-foreground">
                      {corr.replay_impact.summary}
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* Tool Chain Findings */}
      {hasToolChain && (
        <div className={hasCorrelation ? 'mt-5' : ''}>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-[11px] font-medium uppercase tracking-wider" style={{ color: 'var(--text-tertiary)' }}>
              Tool Chain
            </span>
            <span
              className="text-[11px] font-medium px-2.5 py-0.5 rounded-full"
              style={{
                color: 'var(--warning)',
                backgroundColor: 'color-mix(in srgb, var(--warning) 10%, transparent)',
                border: '1px solid color-mix(in srgb, var(--warning) 40%, transparent)',
              }}
            >
              {tcFindings.length} finding{tcFindings.length !== 1 ? 's' : ''}
            </span>
          </div>

          <div className="mt-4 rounded-[8px] border border-border bg-background p-4">
            <div className="space-y-4">
              {tcFindings.map((f, i) => (
                <ToolChainRow key={i} finding={f} />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ToolChainRow({ finding }: { finding: ToolChainFinding }) {
  const sevColor = finding.severity === 'critical' ? 'var(--failure)' : 'var(--warning)'

  return (
    <div>
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className="text-[10px] font-bold px-2 py-0.5 rounded-full"
          style={{
            color: sevColor,
            backgroundColor: `color-mix(in srgb, ${sevColor} 10%, transparent)`,
            border: `1px solid color-mix(in srgb, ${sevColor} 30%, transparent)`,
          }}
        >
          {finding.finding_id}
        </span>
        <span
          className="text-[10px] px-2 py-0.5 rounded-full text-muted-foreground"
          style={{ background: 'var(--hover)' }}
        >
          {FINDING_LABELS[finding.finding_type] ?? finding.finding_type}
        </span>
        <div className="flex items-center gap-1.5">
          {finding.nodes_involved.map((node, ni) => (
            <span key={ni} className="flex items-center gap-1.5">
              <span
                className="font-mono text-[12px] font-semibold px-2 py-0.5 rounded text-foreground"
                style={{ background: 'var(--hover)' }}
              >
                {node}
              </span>
              {ni < finding.nodes_involved.length - 1 && (
                <span className="text-[10px] text-muted-foreground/50">→</span>
              )}
            </span>
          ))}
        </div>
      </div>
      <p className="mt-1.5 text-[12px] text-muted-foreground">{finding.description}</p>
      <p className="mt-0.5 text-[11px] text-muted-foreground/60 italic">{finding.evidence}</p>
    </div>
  )
}
