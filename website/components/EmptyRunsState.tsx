'use client'

/* Empty state — spec `.empty`: dashed frame, a glyph, one sentence, one
   primary action. The demo command is the primary action; attaching to a
   real graph is the secondary. */

import { useState } from 'react'
import Link from 'next/link'
import type { ServingInfo } from '@/lib/hooks'

function pathsDiffer(a?: string, b?: string): boolean {
  if (!a || !b) return false
  const norm = (p: string) => p.replace(/\/+$/, '')
  return norm(a) !== norm(b)
}

const DEMO_CMD = 'argus demo --open'

export default function EmptyRunsState({ serving }: { serving: ServingInfo | null }) {
  const wrongDir = pathsDiffer(serving?.cwd, serving?.project_root)
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(DEMO_CMD)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="empty" style={{ maxWidth: 560, margin: '40px auto 0', width: '100%' }}>
      <svg className="empty-glyph" width="34" height="34" viewBox="0 0 18 18" fill="none" aria-hidden="true">
        <path d="M9 1.5 16.5 5.5v7L9 16.5 1.5 12.5v-7L9 1.5Z" stroke="var(--ink-3)" strokeWidth="1.2" />
        <circle cx="9" cy="9" r="2.2" stroke="var(--ink-3)" strokeWidth="1.1" />
        <circle cx="9" cy="9" r=".9" fill="var(--ink-3)" />
      </svg>
      <h4>{wrongDir ? 'No runs in the directory this UI is serving' : 'No runs recorded yet'}</h4>
      <p>
        Wrap your graph with <code>ArgusWatcher(graph)</code> and run it once. Findings appear here within a
        second of the run finishing.
      </p>
      <div style={{ display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap' }}>
        <button type="button" className="btn btn-primary" onClick={copy} title="Copy to clipboard">
          <code style={{ background: 'transparent', color: 'inherit', padding: 0, fontSize: 12.5 }}>{DEMO_CMD}</code>
          <span style={{ opacity: 0.8, fontWeight: 500 }}>{copied ? '· copied' : '· copy'}</span>
        </button>
        <Link href="/guide" className="btn">Attach to my graph</Link>
      </div>
      {serving?.runs_dir && (
        <p style={{ marginTop: 22, marginBottom: 0, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--ink-4)', wordBreak: 'break-all' }}>
          reading from {serving.runs_dir}
          {wrongDir && serving.cwd && serving.project_root && (
            <><br />started from {serving.cwd} · project root {serving.project_root}</>
          )}
        </p>
      )}
    </div>
  )
}
