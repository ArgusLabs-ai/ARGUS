/* Execution graph model — pure derivations for ExecutionGraph.tsx: node
   kinds, status mapping, the signal satellites docked under each node, and
   the DAG layout. Geometry follows the Argus Instrument spec (05). */

import type { NodeEvent, RunRecord, StepStatus } from './types'
import { getFailureMeta } from './failure-labels'

export const W = 178          // node width — spec
export const NODE_H = 56      // fallback node height before measurement
export const TOOL_GAP_Y = 30  // node bottom → satellite row — spec
export const BUS_Y = 16       // node bottom → bus line — spec
export const PAD = 44
const GAP_X = 96
const ROW_GAP = 26          // between stacked nodes
const PILL_BAND = TOOL_GAP_Y + 25 + 4  // extra room a satellite row needs
const MAX_PILLS = 3

/* ── kinds ───────────────────────────────────────────────────── */

export type NodeKind = 'trigger' | 'transform' | 'retrieval' | 'llm' | 'tool' | 'guard' | 'output' | 'default'

export function inferKind(name: string): NodeKind {
  const n = name.toLowerCase()
  if (/(ingest|start|trigger|input|entry)/.test(n)) return 'trigger'
  if (/(fetch|retriev|search|query|load|source)/.test(n)) return 'retrieval'
  if (/(summar|synth|generat|plan|llm|model|revise|draft|answer)/.test(n)) return 'llm'
  if (/(verify|validat|check|guard|review)/.test(n)) return 'guard'
  if (/(final|output|report|emit|render|send)/.test(n)) return 'output'
  if (/(merge|map|transform|parse|format|normal|convert)/.test(n)) return 'transform'
  if (/(tool|call|api|http)/.test(n)) return 'tool'
  return 'default'
}

/* ── status ──────────────────────────────────────────────────── */

export type GStatus = 'pass' | 'fail' | 'crashed' | 'semantic' | 'degraded' | 'running' | 'skipped'

export const STATUS_META: Record<GStatus, { cls: string; label: string; color: string; chip: string }> = {
  pass:     { cls: 's-pass',     label: 'passed',         color: 'var(--ok)',          chip: 'chip-ok' },
  fail:     { cls: 's-fail',     label: 'silent failure', color: 'var(--quality)',     chip: 'chip-quality' },
  crashed:  { cls: 's-crashed',  label: 'crashed',        color: 'var(--tool)',        chip: 'chip-tool' },
  semantic: { cls: 's-semantic', label: 'semantic fail',  color: 'var(--semantic)',    chip: 'chip-semantic' },
  degraded: { cls: 's-degraded', label: 'degraded input', color: 'var(--coherence)',   chip: 'chip-coherence' },
  running:  { cls: 's-running',  label: 'interrupted',    color: 'var(--iris-bright)', chip: 'chip-run' },
  skipped:  { cls: 's-skipped',  label: 'not reached',    color: 'var(--ink-3)',       chip: 'chip-idle' },
}

const SEVERITY: Partial<Record<GStatus, number>> = { crashed: 4, semantic: 3, fail: 2, degraded: 1 }

export const EDGE_COLOR: Record<GStatus, string> = {
  pass: 'var(--edge-pass)', running: 'var(--iris)', skipped: 'var(--edge-skip)',
  crashed: 'var(--tool)', semantic: 'var(--semantic)', fail: 'var(--quality)',
  degraded: 'var(--coherence)',
}

export function mapStatus(s: StepStatus | undefined): GStatus {
  switch (s) {
    case 'pass': return 'pass'
    case 'crashed': return 'crashed'
    case 'semantic_fail': return 'semantic'
    case 'degraded_input': return 'degraded'
    case 'fail': case 'retried': return 'fail'
    case 'interrupted': return 'running'
    case 'skipped': return 'skipped'
    /* A status this UI does not know yet must not render as green. */
    default: return 'skipped'
  }
}

export function edgeState(a: GStatus, b: GStatus): GStatus {
  if (a === 'running' || b === 'running') return 'running'
  if (a === 'skipped' && b === 'skipped') return 'skipped'
  const sa = SEVERITY[a] ?? 0
  const sb = SEVERITY[b] ?? 0
  if (sa === 0 && sb === 0) return 'pass'
  return sa >= sb ? a : b
}

/* ── signal satellites ────────────────────────────────────────
   The spec docks real tool calls under a node. Dev-branch runs carry no
   tool records, so a satellite is one *field* the node was flagged on —
   every signal on that field collapses into a single pill, worst first. */

export type PillTone = 'error' | 'slow' | 'sem' | 'empty' | 'more'

export interface PillSignal { tag: string; label: string; severity: 'critical' | 'warning'; evidence: string }

export interface Pill {
  id: string            // short display name
  field: string         // full field path
  tag: string           // short code shown after the name
  tone: PillTone
  signals: PillSignal[]
}

function shortField(path: string): string {
  const segs = path.split('.').filter((s) => s && !/^\[\d+\]$/.test(s))
  if (!segs.length) return 'output'
  const last = segs[segs.length - 1]
  const name = last.length <= 12 && segs.length > 1 ? `${segs[segs.length - 2]}.${last}` : last
  return name.length > 20 ? `${name.slice(0, 19)}…` : name
}

const TONE_RANK: Record<PillTone, number> = { error: 0, sem: 1, slow: 2, empty: 3, more: 4 }

function toneOf(category: string, severity: string): PillTone {
  if (severity === 'critical') return 'error'
  if (category === 'Semantic') return 'sem'
  if (category === 'Coherence') return 'empty'
  return 'slow'
}

export function pillsFor(step: NodeEvent | undefined): Pill[] {
  if (!step) return []
  const insp = step.inspection
  const byField = new Map<string, Pill>()
  const add = (field: string, s: PillSignal, tone: PillTone) => {
    const key = field || 'output'
    const cur = byField.get(key)
    if (!cur) {
      byField.set(key, { id: shortField(key), field: key, tag: s.tag, tone, signals: [s] })
      return
    }
    cur.signals.push(s)
    if (TONE_RANK[tone] < TONE_RANK[cur.tone]) cur.tone = tone
    /* A signature code (CM-008) reads better than a label once both exist. */
    if (/^[A-Z]{2}-\d+$/.test(s.tag) && !/^[A-Z]{2}-\d+$/.test(cur.tag)) cur.tag = s.tag
  }

  for (const tf of insp?.tool_failures ?? []) {
    const meta = getFailureMeta(tf.failure_type)
    add(tf.field_name, { tag: meta.label, label: `${meta.category} · ${meta.label}`, severity: tf.severity, evidence: tf.evidence },
      toneOf(meta.category, tf.severity))
  }
  for (const sig of insp?.semantic_signals ?? []) {
    add(sig.field_path?.join('.') ?? '', { tag: sig.sig_id, label: sig.description, severity: sig.severity, evidence: sig.evidence },
      toneOf('Semantic', sig.severity))
  }
  for (const an of step.anomaly_signals ?? []) {
    add(an.field_path || 'behaviour', { tag: an.anomaly_id, label: an.reason, severity: an.severity, evidence: an.observed_behavior },
      toneOf('Quality', an.severity))
  }
  for (const f of insp?.missing_fields ?? []) {
    add(f, { tag: 'missing', label: 'Missing required field', severity: 'critical', evidence: `\`${f}\` was not in the update` }, 'error')
  }

  const all = Array.from(byField.values()).sort((a, b) => TONE_RANK[a.tone] - TONE_RANK[b.tone])
  if (all.length <= MAX_PILLS) return all
  const shown = all.slice(0, MAX_PILLS - 1)
  const rest = all.slice(MAX_PILLS - 1)
  return [...shown, {
    id: `+${rest.length}`, field: '', tag: 'more', tone: 'more',
    signals: rest.flatMap((p) => p.signals),
  }]
}

/* Rough pill width for layout spacing (10.5 px mono ≈ 6.3 px/char). The
   bus is drawn from measured DOM; this only keeps columns from colliding. */
function pillWidth(p: Pill): number {
  return 22 + 11 + 6 + (p.id.length + p.tag.length + 1) * 6.3
}
export function pillRowWidth(pills: Pill[]): number {
  if (!pills.length) return 0
  return pills.reduce((s, p) => s + pillWidth(p), 0) + 7 * (pills.length - 1)
}

/* ── layout ──────────────────────────────────────────────────── */

function dagLayers(names: string[], edgeMap: Record<string, string[]>): string[][] {
  const indeg: Record<string, number> = {}
  names.forEach((n) => { indeg[n] = 0 })
  for (const tos of Object.values(edgeMap ?? {})) {
    for (const t of tos) if (t in indeg) indeg[t] += 1
  }
  const seen = new Set<string>()
  const layers: string[][] = []
  let ready = names.filter((n) => indeg[n] === 0)
  if (!ready.length) ready = names.slice(0, 1)

  while (ready.length && seen.size < names.length) {
    const layer = ready.filter((n) => !seen.has(n))
    if (!layer.length) break
    layers.push(layer)
    layer.forEach((n) => seen.add(n))
    const next = new Set<string>()
    for (const n of layer) {
      for (const t of edgeMap?.[n] ?? []) {
        if (seen.has(t)) continue
        indeg[t] -= 1
        if (indeg[t] <= 0) next.add(t)
      }
    }
    ready = Array.from(next)
  }
  const left = names.filter((n) => !seen.has(n))
  if (left.length) layers.push(left)
  return layers.length ? layers : [names]
}

export interface GNode {
  id: string; kind: NodeKind; status: GStatus; ms: number | null
  x: number; y: number; isRoot: boolean; pills: Pill[]
}

export function layoutGraph(run: RunRecord, names: string[], edgeMap: Record<string, string[]>): GNode[] {
  const layers = dagLayers(names, edgeMap)
  const root = run.root_cause_chain?.[0] ?? null
  const stepFor = (n: string) => (run.steps ?? []).find((s) => s.node_name === n)
  const cols = layers.map((layer) => layer.map((id) => {
    const st = stepFor(id)
    return { id, st, pills: pillsFor(st) }
  }))
  /* Rows are only as tall as they need to be: a node with satellites
     reserves their band, a bare node packs tight. Columns centre on the
     tallest one. */
  const pitch = (c: { pills: unknown[] }) => NODE_H + ROW_GAP + (c.pills.length ? PILL_BAND : 0)
  /* The last node's satellite band hangs below the column; leaving it out
     keeps a linear chain on one baseline. */
  const colH = cols.map((col) => col.reduce((h, c) => h + pitch(c), 0) - (col[col.length - 1]?.pills.length ? PILL_BAND : 0))
  const tallest = Math.max(0, ...colH)
  const out: GNode[] = []
  let x = PAD
  cols.forEach((col, ci) => {
    /* A column is as wide as its widest satellite row, so pills never run
       under the next column's nodes. */
    const colW = Math.max(W, ...col.map((c) => pillRowWidth(c.pills)))
    let y = PAD + 20 + (tallest - colH[ci]) / 2
    col.forEach((c) => {
      const status = mapStatus(c.st?.status)
      out.push({
        id: c.id,
        kind: inferKind(c.id),
        /* Blamed but reported pass: that is the silent failure itself. */
        status: c.id === root && status === 'pass' ? 'fail' : status,
        ms: c.st ? Math.round(c.st.duration_ms) : null,
        x,
        y,
        isRoot: c.id === root,
        pills: c.pills,
      })
      y += pitch(c)
    })
    x += colW + GAP_X
  })
  return out
}

/** Every node reachable from `id` in either direction — the spec's
    "follow cause" set. Everything outside it dims on select. */
export function related(id: string, edgeMap: Record<string, string[]>): Set<string> {
  const keep = new Set([id])
  let grew = true
  while (grew) {
    grew = false
    for (const [a, tos] of Object.entries(edgeMap)) {
      for (const b of tos) {
        if (keep.has(a) && !keep.has(b)) { keep.add(b); grew = true }
        if (keep.has(b) && !keep.has(a)) { keep.add(a); grew = true }
      }
    }
  }
  return keep
}

/** Shortest node path a → b through the edge map, inclusive; [a, b] if none. */
export function pathBetween(edgeMap: Record<string, string[]>, a: string, b: string): string[] {
  if (a === b) return [a]
  const prev = new Map<string, string>([[a, a]])
  const queue = [a]
  while (queue.length) {
    const n = queue.shift()!
    for (const t of edgeMap[n] ?? []) {
      if (prev.has(t)) continue
      prev.set(t, n)
      if (t === b) {
        const path = [b]
        let cur = b
        while (cur !== a) { cur = prev.get(cur)!; path.unshift(cur) }
        return path
      }
      queue.push(t)
    }
  }
  return [a, b]
}
