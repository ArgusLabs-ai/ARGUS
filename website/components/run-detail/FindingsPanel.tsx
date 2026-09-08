'use client'

/* Findings as rows on hairlines: a severity dot, one sentence, and a
   quiet mono meta line. "Fix prompt" fetches the real `argus fix` markdown
   and opens it flush under the row. */

import { useState } from 'react'
import type { Finding } from '@/lib/types'
import Prose from './Prose'
import FixPromptButton from './FixPrompt'

const SEV_COLOR: Record<string, string> = {
  critical: 'var(--tool)',
  warning: 'var(--quality)',
  info: 'var(--iris)',
}
const SEV_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 }

export default function FindingsPanel({
  findings,
  runId,
  onSelectNode,
  culprit,
}: {
  findings: Finding[]
  runId: string
  onSelectNode: (node: string) => void
  culprit?: string | null
}) {
  const [showSuppressed, setShowSuppressed] = useState(false)
  if (!findings.length) return null

  const active = findings
    .filter((f) => !f.suppressed)
    .sort((a, b) => (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3))
  const suppressed = findings.filter((f) => f.suppressed)
  const crit = active.filter((f) => f.severity === 'critical').length
  const warn = active.filter((f) => f.severity === 'warning').length

  const rows = showSuppressed ? [...active, ...suppressed] : active

  return (
    <div>
      <p className="cap">
        <span>
          Findings · {active.length}
          {crit > 0 && <> · <span style={{ color: 'var(--tool)' }}>{crit} critical</span></>}
          {warn > 0 && <> · {warn} warning{warn === 1 ? '' : 's'}</>}
        </span>
        {suppressed.length > 0 && (
          <a href="#" onClick={(e) => { e.preventDefault(); setShowSuppressed((v) => !v) }}>
            {showSuppressed ? 'Hide suppressed' : `${suppressed.length} suppressed`}
          </a>
        )}
      </p>
      <div className="flist">
        {rows.map((f) => (
          <div
            key={f.id}
            role="button"
            tabIndex={0}
            className={`frow${f.suppressed ? ' off' : ''}`}
            onClick={() => onSelectNode(f.node)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectNode(f.node) } }}
          >
            <span className="frow-sev" style={{ background: SEV_COLOR[f.severity] ?? 'var(--ink-4)' }} />
            <div style={{ minWidth: 0 }}>
              <p className="frow-text">
                <Prose text={f.reason} who={culprit} />
              </p>
              <p className="frow-meta">
                <span>{f.node}</span>
                {f.origin_node && f.origin_node !== f.node && <span>← {f.origin_node}</span>}
                <span>{f.type}</span>
                <span>{f.source}</span>
                {typeof f.confidence === 'number' && <span>{f.confidence.toFixed(2)}</span>}
              </p>
            </div>
            {!f.suppressed && <FixPromptButton runId={runId} node={f.origin_node ?? f.node} />}
          </div>
        ))}
      </div>
    </div>
  )
}
