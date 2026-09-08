'use client'

/* Workspace state for the IDE frame: the run list the explorer reads, the
   strip of open tabs, which tab is active (derived from the URL), and the
   live-tail toggle. Tabs persist across reloads in localStorage. */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import type { RunSummary } from './types'
import { useServingInfo, type ServingInfo } from './hooks'

export type WsTab =
  | { id: string; kind: 'run'; runId: string }
  | { id: string; kind: 'compare'; a: string; b: string }

export function compareTabId(a: string, b: string): string {
  return `cmp:${a}:${b}`
}

/** Last segment of a timestamped run id: `20260815-224711-2e8a3c` → `2e8a3c`. */
export function shortRunId(id: string): string {
  const parts = id.split('-')
  const tail = parts[parts.length - 1]
  return tail && tail.length >= 4 && tail.length <= 8 ? tail : id.slice(-6)
}

interface WorkspaceValue {
  runs: RunSummary[]
  runsLoading: boolean
  refreshRuns: () => void
  serving: ServingInfo | null
  live: boolean
  setLive: (v: boolean) => void

  tabs: WsTab[]
  activeTabId: string | null
  activeRunId: string | null
  openRun: (runId: string, opts?: { replace?: boolean }) => void
  openCompare: (a: string, b: string) => void
  closeTab: (tabId: string) => void
  goHome: () => void

  note: { text: string; key: string } | null
  setNote: (n: { text: string; key: string } | null) => void
  dismissNote: () => void
}

const Ctx = createContext<WorkspaceValue | null>(null)
const TABS_KEY = 'argus-ws-tabs'
const POLL_MS = 6000

function readTabs(): WsTab[] {
  try {
    const raw = localStorage.getItem(TABS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as WsTab[]
    return Array.isArray(parsed) ? parsed.filter((t) => t && typeof t.id === 'string') : []
  } catch {
    return []
  }
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const serving = useServingInfo()

  /* ── run list ── */
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [runsLoading, setRunsLoading] = useState(true)
  const [live, setLiveState] = useState(true)

  const fetchRuns = useCallback(() => {
    fetch('/api/runs', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('unavailable'))))
      .then((data: RunSummary[]) => {
        setRuns((prev) => {
          if (prev.length === data.length && prev.every((p, i) => p.run_id === data[i]?.run_id && p.overall_status === data[i]?.overall_status)) {
            return prev
          }
          return data
        })
        setRunsLoading(false)
      })
      .catch(() => setRunsLoading(false))
  }, [])

  useEffect(() => { fetchRuns() }, [fetchRuns])
  useEffect(() => {
    if (!live) return
    const t = setInterval(fetchRuns, POLL_MS)
    return () => clearInterval(t)
  }, [live, fetchRuns])

  const setLive = useCallback((v: boolean) => {
    setLiveState(v)
    if (v) fetchRuns()
  }, [fetchRuns])

  /* ── tabs ── */
  const [tabs, setTabs] = useState<WsTab[]>([])
  const hydrated = useRef(false)
  useEffect(() => {
    setTabs(readTabs())
    hydrated.current = true
  }, [])
  useEffect(() => {
    if (!hydrated.current) return
    try { localStorage.setItem(TABS_KEY, JSON.stringify(tabs)) } catch { /* ignore */ }
  }, [tabs])

  const activeRunId = pathname === '/' ? searchParams.get('run') : null
  const cmpA = pathname === '/compare' ? searchParams.get('a') : null
  const cmpB = pathname === '/compare' ? searchParams.get('b') : null
  const activeTabId = activeRunId ? `run:${activeRunId}` : cmpA && cmpB ? compareTabId(cmpA, cmpB) : null

  /* A deep link to a run or a comparison opens its tab. */
  useEffect(() => {
    if (!hydrated.current) return
    if (activeRunId) {
      const id = `run:${activeRunId}`
      setTabs((prev) => (prev.some((t) => t.id === id) ? prev : [...prev, { id, kind: 'run', runId: activeRunId }]))
    } else if (cmpA && cmpB) {
      const id = compareTabId(cmpA, cmpB)
      setTabs((prev) => (prev.some((t) => t.id === id) ? prev : [...prev, { id, kind: 'compare', a: cmpA, b: cmpB }]))
    }
  }, [activeRunId, cmpA, cmpB])

  const openRun = useCallback((runId: string, opts?: { replace?: boolean }) => {
    const id = `run:${runId}`
    setTabs((prev) => (prev.some((t) => t.id === id) ? prev : [...prev, { id, kind: 'run', runId }]))
    const href = `/?run=${encodeURIComponent(runId)}`
    if (opts?.replace) router.replace(href, { scroll: false })
    else router.push(href, { scroll: false })
  }, [router])

  const openCompare = useCallback((a: string, b: string) => {
    const id = compareTabId(a, b)
    setTabs((prev) => (prev.some((t) => t.id === id) ? prev : [...prev, { id, kind: 'compare', a, b }]))
    router.push(`/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`, { scroll: false })
  }, [router])

  const goHome = useCallback(() => {
    router.push('/', { scroll: false })
  }, [router])

  const closeTab = useCallback((tabId: string) => {
    setTabs((prev) => {
      const idx = prev.findIndex((t) => t.id === tabId)
      if (idx === -1) return prev
      const next = prev.filter((t) => t.id !== tabId)
      if (tabId === activeTabId) {
        const neighbour = next[idx] ?? next[idx - 1]
        if (!neighbour) router.push('/', { scroll: false })
        else if (neighbour.kind === 'run') router.push(`/?run=${encodeURIComponent(neighbour.runId)}`, { scroll: false })
        else router.push(`/compare?a=${encodeURIComponent(neighbour.a)}&b=${encodeURIComponent(neighbour.b)}`, { scroll: false })
      }
      return next
    })
  }, [activeTabId, router])

  /* ── workspace note (the pill at the top right) ── */
  const [note, setNoteState] = useState<{ text: string; key: string } | null>(null)
  const dismissed = useRef<Set<string>>(new Set())
  const setNote = useCallback((n: { text: string; key: string } | null) => {
    if (n && dismissed.current.has(n.key)) { setNoteState(null); return }
    setNoteState(n)
  }, [])
  const dismissNote = useCallback(() => {
    setNoteState((n) => { if (n) dismissed.current.add(n.key); return null })
  }, [])

  const value = useMemo<WorkspaceValue>(() => ({
    runs, runsLoading, refreshRuns: fetchRuns, serving, live, setLive,
    tabs, activeTabId, activeRunId, openRun, openCompare, closeTab, goHome,
    note, setNote, dismissNote,
  }), [runs, runsLoading, fetchRuns, serving, live, setLive, tabs, activeTabId, activeRunId, openRun, openCompare, closeTab, goHome, note, setNote, dismissNote])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useWorkspace(): WorkspaceValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useWorkspace must be used inside WorkspaceProvider')
  return v
}

/* ── status → glyph class used by explorer items, tabs and rows ── */
export type StatusTone = 'ok' | 'bad' | 'warn' | 'sem' | 'live' | 'mute'

export function toneFor(status: string | undefined): StatusTone {
  switch (status) {
    case 'clean': return 'ok'
    case 'crashed': return 'bad'
    case 'silent_failure': return 'warn'
    case 'semantic_fail': return 'sem'
    case 'interrupted': return 'live'
    default: return 'mute'
  }
}

export function statusWord(status: string | undefined): string {
  switch (status) {
    case 'clean': return 'clean'
    case 'crashed': return 'crashed'
    case 'silent_failure': return 'silent failure'
    case 'semantic_fail': return 'semantic fail'
    case 'interrupted': return 'interrupted'
    default: return status ?? '—'
  }
}

export function relativeAge(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.max(0, Math.floor(diff / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  const d = Math.floor(h / 24)
  return `${d}d`
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}
