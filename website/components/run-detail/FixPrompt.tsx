'use client'

/* The paste-ready coding-agent prompt `argus fix` emits. On a failing run
   the overview loads it immediately so it is on the page, not behind a click.
   Copy still goes through the clipboard. */

import { useCallback, useEffect, useState } from 'react'
import { fetchFixPrompt, type FixPromptPayload } from '@/lib/fix-prompt'

export function FixPromptBody({
  node,
  sourcePath,
  prompt,
  error,
  copied,
  busy,
  onCopy,
  onHide,
}: {
  node?: string | null
  sourcePath?: string | null
  prompt?: string | null
  error?: string | null
  copied?: boolean
  busy?: boolean
  onCopy?: () => void
  onHide?: () => void
}) {
  if (error) {
    return <p className="note-line bad" style={{ margin: '8px 0 0' }}>{error}</p>
  }
  if (busy && !prompt) {
    return <p className="note-line" style={{ margin: '8px 0 0' }}>Building the fix prompt…</p>
  }
  if (!prompt) return null
  return (
    <div className="fix-panel">
      <p className="cap">
        <span>
          Fix prompt · paste into a coding agent · <span style={{ fontFamily: 'var(--mono)' }}>{node}</span>
          {sourcePath && <> · {sourcePath}</>}
        </span>
        <span style={{ display: 'flex', gap: 14 }}>
          {onCopy && <a href="#" onClick={(e) => { e.preventDefault(); onCopy() }}>{copied ? 'Copied' : 'Copy'}</a>}
          {onHide && <a href="#" onClick={(e) => { e.preventDefault(); onHide() }}>Hide</a>}
        </span>
      </p>
      <pre className="trace">{prompt}</pre>
    </div>
  )
}

export function useFixPrompt(runId: string, node?: string | null, opts?: { autoload?: boolean }) {
  const autoload = opts?.autoload ?? false
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [open, setOpen] = useState(false)
  const [payload, setPayload] = useState<FixPromptPayload | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setOpen(false)
    setPayload(null)
    setError(null)
    setCopied(false)
    if (!runId || !autoload) return
    let cancelled = false
    setBusy(true)
    fetchFixPrompt(runId, node)
      .then((data) => {
        if (cancelled) return
        setPayload(data)
        setOpen(true)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Could not build a fix prompt')
        setOpen(true)
      })
      .finally(() => { if (!cancelled) setBusy(false) })
    return () => { cancelled = true }
  }, [runId, node, autoload])

  const copyText = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch { /* clipboard can fail in some embeds; the prompt is still shown */ }
  }, [])

  const load = useCallback(async () => {
    if (payload && !error) {
      setOpen(true)
      await copyText(payload.prompt)
      return
    }
    if (!runId) return
    setBusy(true)
    setError(null)
    try {
      const data = await fetchFixPrompt(runId, node)
      setPayload(data)
      setOpen(true)
      await copyText(data.prompt)
    } catch (err) {
      setPayload(null)
      setError(err instanceof Error ? err.message : 'Could not build a fix prompt')
      setOpen(true)
    } finally {
      setBusy(false)
    }
  }, [runId, node, payload, error, copyText])

  const copy = useCallback(() => {
    if (payload?.prompt) {
      setOpen(true)
      void copyText(payload.prompt)
      return
    }
    void load()
  }, [payload, copyText, load])

  const label = busy ? 'Building…' : copied ? 'Copied' : 'Copy fix prompt'
  return { load, copy, busy, copied, open, payload, error, label, setOpen }
}

export default function FixPromptButton({
  runId,
  node,
  className = 'frow-fix',
  showPanel = true,
}: {
  runId: string
  node?: string | null
  className?: string
  showPanel?: boolean
}) {
  const fix = useFixPrompt(runId, node)
  return (
    <>
      <button
        type="button"
        className={className}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); void fix.load() }}
        aria-expanded={showPanel ? fix.open : undefined}
        aria-label={node ? `Fix prompt for ${node}` : 'Copy fix prompt for the root-cause node'}
      >
        {fix.label}
      </button>
      {showPanel && fix.open && (
        <div className="fix-slot" onClick={(e) => e.stopPropagation()}>
          <FixPromptBody
            node={fix.payload?.node ?? node}
            sourcePath={fix.payload?.source_path}
            prompt={fix.payload?.prompt}
            error={fix.error}
            copied={fix.copied}
            busy={fix.busy}
            onCopy={fix.copy}
            onHide={() => fix.setOpen(false)}
          />
        </div>
      )}
    </>
  )
}
