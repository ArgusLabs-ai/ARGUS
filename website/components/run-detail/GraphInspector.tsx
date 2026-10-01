'use client'

/* The graph's inspector overlay (spec 05): a node's status, timing, its
   lead finding and signals, and — on the blame path — the propagation
   chain; or, for a satellite, every signal recorded on that field. */

import type { RunRecord } from '@/lib/types'
import { formatDuration } from '@/lib/workspace'
import { activeFindings } from '@/lib/run-detail'
import { findingMeta } from '@/lib/failure-labels'
import { STATUS_META, type GNode, type Pill, type PillTone } from '@/lib/graph-model'
import Prose from './Prose'

const TONE_COLOR: Record<PillTone, string> = {
  error: 'var(--tool)', sem: 'var(--semantic)', slow: 'var(--quality)', empty: 'var(--coherence)', more: 'var(--ink-3)',
}

function Line({ k, v, color }: { k: string; v: React.ReactNode; color?: string }) {
  return <div className="insp-line"><span>{k}</span><span style={color ? { color } : undefined}>{v}</span></div>
}

export default function GraphInspector({
  run, node, pill, prop, onClose, onOpenDetails,
}: {
  run: RunRecord
  node: GNode
  pill: Pill | null
  prop: string[]
  onClose: () => void
  onOpenDetails?: () => void
}) {
  const m = STATUS_META[node.status]
  const own = activeFindings(run).filter((f) => (f.origin_node ?? f.node) === node.id || f.node === node.id)
  const lead = own[0]
  const onPath = prop.includes(node.id)

  return (
    <aside className="ginsp" onPointerDown={(e) => e.stopPropagation()}>
      <div className="ginsp-h">
        <div style={{ minWidth: 0 }}>
          <h5>{pill ? (pill.field || pill.id) : node.id}</h5>
          <div className="sub">
            {pill ? <>Signal on <span className="mono">{node.id}</span></> : <>{node.kind} node{node.isRoot ? ' · root cause' : ''}</>}
          </div>
        </div>
        <button type="button" className="ginsp-x" aria-label="Close inspector" onClick={onClose}>✕</button>
      </div>

      <div className="ginsp-b">
        {pill ? (
          pill.signals.map((s, i) => (
            <div key={i} className="ginsp-sec">
              <div className="ginsp-sig">
                <span className="mono" style={{ color: s.severity === 'critical' ? 'var(--tool)' : TONE_COLOR[pill.tone] }}>{s.tag}</span>
                <span className="ginsp-sev">{s.severity}</span>
              </div>
              <p className="ginsp-p">{s.label}</p>
              {s.evidence && <div className="insp-code">{s.evidence}</div>}
            </div>
          ))
        ) : (
          <>
            <div className="ginsp-sec">
              <Line k="Status" v={m.label} color={m.color} />
              <Line k="Duration" v={node.status === 'skipped' ? '—' : formatDuration(node.ms)} />
              <Line k="Signals" v={node.pills.reduce((k, p) => k + p.signals.length, 0)} />
              <Line k="Findings" v={own.length} />
            </div>

            {lead && (() => {
              const fm = findingMeta(lead)
              return (
                <div className="ginsp-sec">
                  <div className="specimen-label">Lead finding</div>
                  <span className={`chip ${fm.chip}`}><span className="dot" />{fm.category} · {fm.label}</span>
                  <p className="ginsp-p" style={{ marginTop: 8 }}><Prose text={lead.reason} /></p>
                </div>
              )
            })()}

            {node.pills.length > 0 && (
              <div className="ginsp-sec">
                <div className="specimen-label">Flagged fields</div>
                {node.pills.flatMap((p) => (p.tone === 'more' ? [] : [p])).map((p, i) => (
                  <Line key={i} k={p.id} v={p.tag} color={TONE_COLOR[p.tone]} />
                ))}
              </div>
            )}

            {onPath && prop.length > 1 && (
              <div className="ginsp-sec">
                <div className="specimen-label">Propagation</div>
                <div className="rc-chain" style={{ margin: 0 }}>
                  {prop.map((n, i) => (
                    <span key={n} style={{ display: 'contents' }}>
                      {i > 0 && <span className="rc-arrow">→</span>}
                      <span className={`rc-node${i === 0 ? ' culprit' : ''}${n === node.id ? ' here' : ''}`}>{n}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {onOpenDetails && (
        <div className="ginsp-f">
          <button type="button" className="btn btn-sm" onClick={onOpenDetails}>Step details</button>
        </div>
      )}
    </aside>
  )
}
