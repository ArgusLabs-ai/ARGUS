import type { RunSummary } from './types'

export type FilterKey = 'status' | 'node' | 'origin' | 'pipeline'

export interface RunFilter {
  key: FilterKey
  value: string
}

export const FILTER_KEYS: { key: FilterKey; label: string }[] = [
  { key: 'status', label: 'status' },
  { key: 'node', label: 'node' },
  { key: 'origin', label: 'origin' },
  { key: 'pipeline', label: 'pipeline' },
]

const ALL_KEYS: FilterKey[] = ['status', 'node', 'origin', 'pipeline']

/* ── pipelines ─────────────────────────────────────────────────────
   Runs have no pipeline name; the graph's node list is its identity. */

const SENTINELS = new Set(['__start__', '__end__', 'START', 'END'])

export function pipelineNodes(run: RunSummary): string[] {
  return (run.graph_node_names ?? []).filter((n) => !SENTINELS.has(n))
}

/** Short stable key for a graph shape (djb2 over the node list). */
export function pipelineKey(run: RunSummary): string {
  const names = pipelineNodes(run)
  let h = 5381
  for (const ch of names.join('>')) h = ((h << 5) + h + ch.charCodeAt(0)) | 0
  return (h >>> 0).toString(36)
}

/** Human label for a pipeline: `first → last`. */
export function pipelineLabel(run: RunSummary): string {
  const names = pipelineNodes(run)
  if (names.length === 0) return run.run_id
  if (names.length === 1) return names[0]
  return `${names[0]} → ${names[names.length - 1]}`
}

/* ── URL round-trip ─────────────────────────────────────────────── */

export function filtersFromSearchParams(params: URLSearchParams): RunFilter[] {
  const out: RunFilter[] = []
  for (const key of ALL_KEYS) {
    for (const value of params.getAll(key)) {
      if (value) out.push({ key, value })
    }
  }
  return out
}

export function applyFiltersToParams(
  base: URLSearchParams,
  filters: RunFilter[],
): URLSearchParams {
  const next = new URLSearchParams(base.toString())
  for (const key of ALL_KEYS) next.delete(key)
  for (const f of filters) {
    next.append(f.key, f.value)
  }
  return next
}

function matchesOne(run: RunSummary, f: RunFilter): boolean {
  if (f.key === 'status') return run.overall_status === f.value
  if (f.key === 'node') {
    return (
      run.first_failure_step === f.value ||
      (run.graph_node_names ?? []).includes(f.value) ||
      (run.finding_nodes ?? []).includes(f.value)
    )
  }
  if (f.key === 'origin') {
    return (run.origins ?? []).includes(f.value) || run.first_failure_step === f.value
  }
  if (f.key === 'pipeline') return pipelineKey(run) === f.value
  return true
}

export function runMatchesFilters(run: RunSummary, filters: RunFilter[]): boolean {
  const keys = Array.from(new Set(filters.map((f) => f.key)))
  for (const key of keys) {
    const group = filters.filter((f) => f.key === key)
    if (group.length > 0 && !group.some((f) => matchesOne(run, f))) return false
  }
  return true
}

export function uniqueFilterValues(runs: RunSummary[], key: FilterKey): string[] {
  const values = new Set<string>()
  for (const run of runs) {
    if (key === 'status') {
      values.add(run.overall_status)
    } else if (key === 'node') {
      if (run.first_failure_step) values.add(run.first_failure_step)
      for (const n of run.finding_nodes ?? []) values.add(n)
    } else if (key === 'origin') {
      for (const o of run.origins ?? []) values.add(o)
      if (run.first_failure_step) values.add(run.first_failure_step)
    } else if (key === 'pipeline') {
      values.add(pipelineKey(run))
    }
  }
  return Array.from(values).sort()
}

/** Display text for a filter chip value. Pipelines show their label, not the hash. */
export function filterValueLabel(runs: RunSummary[], f: RunFilter): string {
  if (f.key !== 'pipeline') return f.value
  const match = runs.find((r) => pipelineKey(r) === f.value)
  return match ? pipelineLabel(match) : f.value
}
