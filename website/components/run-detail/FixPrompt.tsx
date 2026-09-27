'use client'

/* The paste-ready coding-agent prompt `argus fix` emits. On a failing run
   the overview loads it immediately so it is on the page, not behind a click.
   Copy still goes through the clipboard. */

import { useCallback, useEffect, useRef, useState } from 'react'
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
  sanitized,
  onToggleValues,
}: {
  node?: string | null
  sourcePath?: string | null
  prompt?: string | null
  error?: string | null
  copied?: boolean
  busy?: boolean
  onCopy?: () => void
  onHide?: () => void
  sanitized?: boolean
  onToggleValues?: () => void
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
          {' · '}
          {/* This prompt is pasted into someone else's model, so whether it
              carries recorded values is stated, not assumed. */}
          {sanitized ? 'shapes only' : 'includes recorded values'}
        </span>
        <span style={{ display: 'flex', gap: 14 }}>
          {onToggleValues && (
            <a href="#" onClick={(e) => { e.preventDefault(); onToggleValues() }}>
              {sanitized ? 'Include values' : 'Strip values'}
            </a>
          )}
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
  const [sanitized, setSanitized] = useState(false)

  /* Every fetch takes a ticket; only the newest ticket may write state or touch
     the clipboard. Switching node/run or unmounting voids outstanding tickets,
     so a slow response for node A can never land in (or be copied from) node B,
     and out-of-order toggle responses can't show values under "shapes only". */
  const ticket = useRef(0)
  const sanitizedRef = useRef(sanitized)
  sanitizedRef.current = sanitized

  useEffect(() => {
    setOpen(false)
    setPayload(null)
    setError(null)
    setCopied(false)
    const id = ++ticket.current
    /* Clear `busy` and void the ticket on cleanup, or a mid-fetch node switch
       leaves the body stuck on "Building the fix prompt…" forever. */
    const cleanup = () => { ticket.current++; setBusy(false) }
    if (!runId || !autoload) return cleanup
    setBusy(true)
    fetchFixPrompt(runId, node, sanitizedRef.current)
      .then((data) => {
        if (ticket.current !== id) return
        setPayload(data)
        setOpen(true)
      })
      .catch((err) => {
        if (ticket.current !== id) return
        setError(err instanceof Error ? err.message : 'Could not build a fix prompt')
        setOpen(true)
      })
      .finally(() => { if (ticket.current === id) setBusy(false) })
    return cleanup
    /* `sanitized` is read through a ref: the toggle refetches in place, and
       re-running this effect would close the panel mid-toggle. */
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
    const id = ++ticket.current
    setBusy(true)
    setError(null)
    try {
      const data = await fetchFixPrompt(runId, node, sanitized)
      if (ticket.current !== id) return
      setPayload(data)
      setOpen(true)
      await copyText(data.prompt)
    } catch (err) {
      if (ticket.current !== id) return
      setPayload(null)
      setError(err instanceof Error ? err.message : 'Could not build a fix prompt')
      setOpen(true)
    } finally {
      if (ticket.current === id) setBusy(false)
    }
  }, [runId, node, sanitized, payload, error, copyText])

  const copy = useCallback(() => {
    if (payload?.prompt) {
      setOpen(true)
      void copyText(payload.prompt)
      return
    }
    void load()
  }, [payload, copyText, load])

  /* Refetch in place rather than letting the autoload effect do it: that effect
     clears `open`, which would close the panel the moment it is toggled. The old
     payload is dropped immediately so the new label never sits over the other
     variant's text, and Copy in the gap fetches the variant the label names. */
  const toggleValues = useCallback(async () => {
    const next = !sanitized
    setSanitized(next)
    setPayload(null)
    if (!runId) return
    const id = ++ticket.current
    setBusy(true)
    setError(null)
    try {
      const data = await fetchFixPrompt(runId, node, next)
      if (ticket.current !== id) return
      setPayload(data)
      setOpen(true)
    } catch (err) {
      if (ticket.current !== id) return
      setError(err instanceof Error ? err.message : 'Could not build a fix prompt')
      setOpen(true)
    } finally {
      if (ticket.current === id) setBusy(false)
    }
  }, [runId, node, sanitized])

  const label = busy ? 'Building…' : copied ? 'Copied' : 'Copy fix prompt'
  return { load, copy, busy, copied, open, payload, error, label, setOpen, sanitized, toggleValues }
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
        /* Without the panel there is nowhere else to show a failure. */
        title={!showPanel && fix.error && !fix.busy ? fix.error : undefined}
      >
        {!showPanel && fix.error && !fix.busy ? 'Fix prompt failed' : fix.label}
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
            sanitized={fix.sanitized}
            onToggleValues={() => { void fix.toggleValues() }}
          />
        </div>
      )}
    </>
  )
}
