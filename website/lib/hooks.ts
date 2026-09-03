'use client'

import { useEffect, useState } from 'react'
import type { RunRecord, RunSummary } from './types'

/* The dashboard is local-only OSS: it reads runs from the `argus ui` server,
   which serves `.argus/runs/` over /api. There is no account and no remote
   backend — see docs for the hosted path, which lives outside this app. */

/* ── useRunList ─────────────────────────────────────────────────── */

export function useRunList() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/runs')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('unavailable'))))
      .then((data: RunSummary[]) => {
        setRuns(data)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  return { runs, loading }
}

/* ── useRunDetail ───────────────────────────────────────────────── */

export function useRunDetail(runId: string | null) {
  const [run, setRun] = useState<RunRecord | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!runId) {
      setRun(null)
      setError(null)
      return
    }

    setLoading(true)
    setError(null)

    fetch(`/api/runs/${runId}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.error) {
          setError('Run not found')
          setLoading(false)
          return
        }
        setRun(data as RunRecord)
        setLoading(false)
      })
      .catch(() => {
        setError('Failed to load run')
        setLoading(false)
      })
  }, [runId])

  return { run, loading, error }
}

/* ── useSearch ──────────────────────────────────────────────────── */

export function useSearch(runs: RunSummary[], query: string): RunSummary[] {
  if (!query.trim()) return runs
  const q = query.toLowerCase().trim()
  return runs.filter((r) =>
    r.run_id.toLowerCase().includes(q) ||
    r.overall_status.toLowerCase().includes(q) ||
    r.graph_node_names.some((n) => n.toLowerCase().includes(q))
  )
}

/* ── useServingInfo ─────────────────────────────────────────────── */

export interface ServingInfo {
  project_root: string
  runs_dir: string
  cwd?: string
}

export function useServingInfo(): ServingInfo | null {
  const [serving, setServing] = useState<ServingInfo | null>(null)

  useEffect(() => {
    fetch('/api/serving')
      .then((r) => (r.ok ? r.json() : null))
      .then((data: ServingInfo | null) => {
        if (data && data.runs_dir) setServing(data)
      })
      .catch(() => {})
  }, [])

  return serving
}
