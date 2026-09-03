'use client'

import { useState } from 'react'
import type { RunRecord, StepStatus } from '@/lib/types'
import { formatDur } from '@/lib/run-utils'
import {
  Zap,
  FileText,
  Sparkles,
  Database,
  Wrench,
  Search,
  ShieldCheck,
  Send,
  Check,
  X,
  Minus,
  AlertTriangle,
  Maximize2,
  type LucideIcon,
} from 'lucide-react'

const SENTINEL = new Set(['__start__', '__end__', 'START', 'END'])

const NODE_W = 168
const NODE_H = 62
const GAP_X = 56
const GAP_Y = 26
const PAD = 28

type NodeKind = 'trigger' | 'transform' | 'retrieval' | 'llm' | 'tool' | 'guard' | 'output' | 'default'

function inferKind(name: string): NodeKind {
  const n = name.toLowerCase()
  if (n.includes('ingest') || n.includes('event') || n.includes('trigger') || n.includes('webhook')) return 'trigger'
  if (n.includes('parse') || n.includes('extract') || n.includes('transform') || n.includes('diff')) return 'transform'
  if (n.includes('embed') || n.includes('retriev') || n.includes('vector') || n.includes('fetch')) return 'retrieval'
  if (n.includes('llm') || n.includes('plan') || n.includes('synth') || n.includes('summar') || n.includes('review') || n.includes('analyz')) return 'llm'
  if (n.includes('tool') || n.includes('code') || n.includes('exec') || n.includes('web') || n.includes('search')) return 'tool'
  if (n.includes('guard') || n.includes('check') || n.includes('test') || n.includes('valid')) return 'guard'
  if (n.includes('respond') || n.includes('output') || n.includes('send') || n.includes('publish')) return 'output'
  return 'default'
}

const kindIcon: Record<NodeKind, LucideIcon> = {
  trigger: Zap,
  transform: FileText,
  retrieval: Database,
  llm: Sparkles,
  tool: Wrench,
  guard: ShieldCheck,
  output: Send,
  default: Search,
}

type MappedStatus = 'succeeded' | 'crashed' | 'failed' | 'semantic_fail' | 'degraded' | 'running' | 'skipped' | 'pending'

function mapStatus(s: StepStatus | undefined): MappedStatus {
  if (!s) return 'pending'
  if (s === 'pass') return 'succeeded'
  if (s === 'crashed') return 'crashed'
  if (s === 'fail') return 'failed'
  if (s === 'semantic_fail') return 'semantic_fail'
  if (s === 'degraded_input') return 'degraded'
  if (s === 'interrupted') return 'running'
  if (s === 'skipped') return 'skipped'
  if (s === 'retried') return 'skipped'
  return 'succeeded'
}

interface LayoutNode {
  id: string
  label: string
  kind: NodeKind
  status: MappedStatus
  col: number
  row: number
  durationMs: number | null
}

function dagLayers(names: string[], edgeMap: Record<string, string[]>) {
  const nodeSet = new Set(names)
  const indegree = new Map(names.map((n) => [n, 0]))
  const outgoing = new Map<string, string[]>()
  for (const n of names) outgoing.set(n, [])

  for (const [src, tgts] of Object.entries(edgeMap ?? {})) {
    if (!nodeSet.has(src)) continue
    for (const tgt of tgts ?? []) {
      if (!nodeSet.has(tgt)) continue
      outgoing.get(src)?.push(tgt)
      indegree.set(tgt, (indegree.get(tgt) ?? 0) + 1)
    }
  }

  const layers: string[][] = []
  let ready = names.filter((n) => (indegree.get(n) ?? 0) === 0)
  const seen = new Set<string>()

  while (ready.length > 0) {
    const layer = ready.filter((n) => !seen.has(n))
    if (layer.length === 0) break
    layers.push(layer)
    for (const n of layer) seen.add(n)
    const next: string[] = []
    for (const n of layer) {
      for (const t of outgoing.get(n) ?? []) {
        indegree.set(t, (indegree.get(t) ?? 0) - 1)
        if ((indegree.get(t) ?? 0) === 0) next.push(t)
      }
    }
    ready = names.filter((n) => next.includes(n))
  }

  const leftovers = names.filter((n) => !seen.has(n))
  if (leftovers.length) layers.push(...leftovers.map((n) => [n]))
  return layers.length ? layers : names.map((n) => [n])
}

function nodePos(col: number, row: number) {
  return {
    x: PAD + col * (NODE_W + GAP_X),
    y: PAD + row * (NODE_H + GAP_Y),
  }
}

const FAILURE_STATUSES = new Set<MappedStatus>(['crashed', 'failed', 'semantic_fail', 'degraded'])

function edgeKind(from: LayoutNode, to: LayoutNode): 'failed' | 'running' | 'active' | 'idle' {
  if (FAILURE_STATUSES.has(from.status) || FAILURE_STATUSES.has(to.status)) return 'failed'
  if (from.status === 'running' || to.status === 'running') return 'running'
  if (from.status === 'succeeded' && to.status === 'succeeded') return 'active'
  return 'idle'
}

const edgeStroke: Record<string, string> = {
  failed: 'var(--tool)',
  running: 'var(--iris)',
  active: 'color-mix(in srgb, var(--iris) 45%, transparent)',
  idle: 'var(--fill-subtle)',
}

const statusStyles: Record<MappedStatus, string> = {
  succeeded:     'border-[var(--fill-subtle)] bg-[var(--hover)] hover:border-[var(--line-2)]',
  crashed:       'border-[color-mix(in srgb, var(--tool) 45%, transparent)] bg-[var(--tool-dim)]',
  failed:        'border-[color-mix(in srgb, var(--quality) 45%, transparent)] bg-[var(--quality-dim)]',
  semantic_fail: 'border-[color-mix(in srgb, var(--semantic) 45%, transparent)] bg-[var(--semantic-dim)]',
  degraded:      'border-[color-mix(in srgb, var(--quality) 45%, transparent)] bg-[var(--quality-dim)]',
  running:       'border-[color-mix(in srgb, var(--iris) 45%, transparent)] bg-[var(--iris-dim)]',
  skipped:       'border-[var(--hover)] border-dashed bg-[var(--hover)] opacity-55',
  pending:       'border-[var(--hover)] border-dashed bg-[var(--hover)] opacity-55',
}

// Color constants per status — reused by glyph, icon bg, and glow
const STATUS_COLOR: Record<MappedStatus, string> = {
  succeeded:     'var(--ok)',
  crashed:       'var(--tool)',
  failed:        'var(--quality)',
  semantic_fail: 'var(--semantic)',
  degraded:      'var(--quality)',
  running:       'var(--iris)',
  skipped:       'var(--ink-3)',
  pending:       'var(--ink-3)',
}

function StatusGlyph({ status }: { status: MappedStatus }) {
  if (status === 'succeeded')
    return <Check className="h-3 w-3" style={{ color: STATUS_COLOR.succeeded }} strokeWidth={2.5} />
  if (status === 'crashed')
    return <X className="h-3 w-3" style={{ color: STATUS_COLOR.crashed }} strokeWidth={2.5} />
  if (status === 'failed')
    return <AlertTriangle className="h-3 w-3" style={{ color: STATUS_COLOR.failed }} strokeWidth={2.5} />
  if (status === 'semantic_fail')
    return <AlertTriangle className="h-3 w-3" style={{ color: STATUS_COLOR.semantic_fail }} strokeWidth={2.5} />
  if (status === 'degraded')
    return <AlertTriangle className="h-3 w-3" style={{ color: STATUS_COLOR.degraded }} strokeWidth={2.5} />
  if (status === 'running')
    return <AlertTriangle className="h-3 w-3 animate-pulse" style={{ color: STATUS_COLOR.running }} strokeWidth={2.5} />
  return <Minus className="h-3 w-3 text-[var(--ink-3)]" strokeWidth={2.5} />
}

function NodeCard({
  node,
  selected,
  onSelect,
}: {
  node: LayoutNode
  selected: boolean
  onSelect: (id: string) => void
}) {
  const Icon = kindIcon[node.kind]
  const { x, y } = nodePos(node.col, node.row)

  const c = STATUS_COLOR[node.status]
  const isError = FAILURE_STATUSES.has(node.status)
  const glowStyle = isError
    ? { boxShadow: `0 0 0 1px ${c}, 0 0 22px -4px ${c}88` }
    : node.status === 'running'
      ? { boxShadow: '0 0 18px -4px color-mix(in srgb, var(--iris) 45%, transparent)' }
      : undefined

  return (
    <button
      onClick={() => onSelect(node.id)}
      style={{ left: x, top: y, width: NODE_W, height: NODE_H, ...glowStyle }}
      className={[
        'absolute flex items-center gap-2.5 rounded-lg border px-3 text-left transition-all',
        statusStyles[node.status],
        selected ? 'ring-2 ring-[var(--iris)] ring-offset-2 ring-offset-[var(--panel)]' : '',
      ].join(' ')}
    >
      <span
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md"
        style={{
          background: isError
            ? `${c}26`
            : node.status === 'running'
              ? 'var(--iris-dim)'
              : 'var(--hover)',
          color: isError
            ? c
            : node.status === 'running'
              ? 'var(--iris)'
              : 'var(--ink)',
        }}
      >
        <Icon className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium text-[var(--ink)]">
          {node.label}
        </span>
        <span className="mt-0.5 flex items-center gap-1 font-mono text-[11px] tabular-nums text-[var(--ink-3)]">
          <StatusGlyph status={node.status} />
          {node.durationMs != null ? formatDur(node.durationMs) : node.status === 'pending' ? 'queued' : '—'}
        </span>
      </span>
    </button>
  )
}

function Legend() {
  const items = [
    { label: 'Succeeded', color: STATUS_COLOR.succeeded },
    { label: 'Crashed', color: STATUS_COLOR.crashed },
    { label: 'Silent Fail', color: STATUS_COLOR.failed },
    { label: 'Semantic', color: STATUS_COLOR.semantic_fail },
    { label: 'Degraded', color: STATUS_COLOR.degraded },
    { label: 'Skipped', color: STATUS_COLOR.skipped },
  ]
  return (
    <div className="hidden items-center gap-3 md:flex">
      {items.map((i) => (
        <span key={i.label} className="flex items-center gap-1.5 text-[11px] text-[var(--ink-3)]">
          <span className="h-2 w-2 rounded-full" style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  )
}

export default function ExecutionGraph({
  run,
  onViewFull,
  onSelectNode,
}: {
  run: RunRecord
  onViewFull?: () => void
  onSelectNode?: (nodeName: string | null) => void
}) {
  const [selectedNode, setSelectedNode] = useState<string | null>(null)

  const names = (run.graph_node_names ?? []).filter((n) => !n.startsWith('__') && !SENTINEL.has(n))
  const stepMap = new Map((run.steps ?? []).map((s) => [s.node_name, s]))
  const layers = dagLayers(names, run.graph_edge_map ?? {})

  const firstFailureIdx = run.first_failure_step
    ? (run.steps ?? []).findIndex((s) => s.node_name === run.first_failure_step)
    : -1

  const layoutNodes: LayoutNode[] = []
  const byId = new Map<string, LayoutNode>()

  for (let col = 0; col < layers.length; col++) {
    const layer = layers[col]
    for (let row = 0; row < layer.length; row++) {
      const name = layer[row]
      const step = stepMap.get(name)
      let status = mapStatus(step?.status)

      if (step && step.status === 'pass' && firstFailureIdx >= 0 && step.step_index > firstFailureIdx && run.overall_status !== 'clean') {
        status = 'succeeded'
      }
      if (!step && firstFailureIdx >= 0) {
        status = 'skipped'
      }

      const node: LayoutNode = {
        id: name,
        label: name.length > 18 ? name.slice(0, 17) + '…' : name,
        kind: inferKind(name),
        status,
        col,
        row,
        durationMs: step?.duration_ms ?? null,
      }
      layoutNodes.push(node)
      byId.set(name, node)
    }
  }

  const edges: { from: string; to: string }[] = []
  for (const [src, tgts] of Object.entries(run.graph_edge_map ?? {})) {
    if (!byId.has(src)) continue
    for (const tgt of tgts ?? []) {
      if (!byId.has(tgt)) continue
      edges.push({ from: src, to: tgt })
    }
  }

  const maxCol = Math.max(0, ...layoutNodes.map((n) => n.col))
  const maxRow = Math.max(0, ...layoutNodes.map((n) => n.row))
  const canvasW = PAD * 2 + (maxCol + 1) * NODE_W + maxCol * GAP_X
  const canvasH = PAD * 2 + (maxRow + 1) * NODE_H + maxRow * GAP_Y

  return (
    <div
      className="overflow-hidden rounded-xl"
      style={{ background: 'var(--overlay)', border: '1px solid var(--hover)' }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{ borderBottom: '1px solid var(--hover)' }}
      >
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-[var(--ink)]">Execution Graph</h2>
          <span
            className="rounded-full px-2 py-0.5 font-mono text-[11px] text-[var(--ink-3)]"
            style={{ background: 'var(--hover)' }}
          >
            {layoutNodes.length} nodes
          </span>
        </div>
        <div className="flex items-center gap-3">
          <Legend />
          {onViewFull && (
            <button
              onClick={onViewFull}
              className="rounded-md p-1.5 text-[var(--ink-3)] transition-colors hover:text-[var(--ink)]"
              style={{ background: 'transparent' }}
            >
              <Maximize2 className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Graph area */}
      <div className="overflow-x-auto" style={{ background: 'var(--fill-subtle)' }}>
        <div style={{ padding: 16 }}>
          <div
            className="relative mx-auto"
            style={{ width: canvasW, height: Math.max(canvasH, 280), minWidth: '100%' }}
          >
            {/* Dotted grid background */}
            <div
              className="pointer-events-none absolute inset-0"
              style={{
                opacity: 0.4,
                backgroundImage: 'radial-gradient(circle at center, var(--line-2) 1px, transparent 1px)',
                backgroundSize: '22px 22px',
              }}
            />

            {/* Edges — SVG bezier curves */}
            <svg className="pointer-events-none absolute inset-0" width={canvasW} height={canvasH}>
              <defs>
                {(['active', 'failed', 'running', 'idle'] as const).map((kind) => (
                  <marker
                    key={kind}
                    id={`arrow-${kind}`}
                    markerWidth="6"
                    markerHeight="6"
                    refX="5"
                    refY="3"
                    orient="auto"
                  >
                    <path d="M0,0 L6,3 L0,6 Z" fill={edgeStroke[kind]} />
                  </marker>
                ))}
              </defs>
              {edges.map((e) => {
                const from = byId.get(e.from)
                const to = byId.get(e.to)
                if (!from || !to) return null
                const fp = nodePos(from.col, from.row)
                const tp = nodePos(to.col, to.row)
                const sx = fp.x + NODE_W
                const sy = fp.y + NODE_H / 2
                const tx = tp.x
                const ty = tp.y + NODE_H / 2
                const colGap = to.col - from.col
                let d: string
                if (colGap > 1) {
                  // ponytail: check if any node sits in intermediate columns near the curve path
                  // and deflect control points to route around them
                  const midY = (sy + ty) / 2
                  let hasObstacle = false
                  for (const n of layoutNodes) {
                    if (n.col > from.col && n.col < to.col) {
                      const np = nodePos(n.col, n.row)
                      const nCenterY = np.y + NODE_H / 2
                      if (Math.abs(nCenterY - midY) < NODE_H) {
                        hasObstacle = true
                        break
                      }
                    }
                  }
                  if (hasObstacle) {
                    // Route below all nodes to avoid overlap
                    const detourY = canvasH - PAD / 2
                    d = `M ${sx},${sy} C ${sx + 40},${detourY} ${tx - 40},${detourY} ${tx},${ty}`
                  } else {
                    const dx = Math.max(28, (tx - sx) / 2)
                    d = `M ${sx},${sy} C ${sx + dx},${sy} ${tx - dx},${ty} ${tx},${ty}`
                  }
                } else {
                  const dx = Math.max(28, (tx - sx) / 2)
                  d = `M ${sx},${sy} C ${sx + dx},${sy} ${tx - dx},${ty} ${tx},${ty}`
                }
                const kind = edgeKind(from, to)
                return (
                  <path
                    key={`${e.from}-${e.to}`}
                    d={d}
                    fill="none"
                    stroke={edgeStroke[kind]}
                    strokeWidth={kind === 'idle' ? 1 : 1.75}
                    strokeDasharray={kind === 'running' ? '5 4' : kind === 'idle' ? '3 4' : undefined}
                    markerEnd={`url(#arrow-${kind})`}
                    className={kind === 'running' ? 'animate-[dash_1s_linear_infinite]' : undefined}
                  />
                )
              })}
            </svg>

            {/* Nodes — absolutely positioned */}
            {layoutNodes.map((n) => (
              <NodeCard
                key={n.id}
                node={n}
                selected={selectedNode === n.id}
                onSelect={(id) => {
                  setSelectedNode((p) => {
                    const next = p === id ? null : id
                    onSelectNode?.(next)
                    return next
                  })
                }}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Scrollbar track */}
      <div
        className="h-2"
        style={{ background: 'var(--hover)', borderTop: '1px solid var(--hover)' }}
      />
    </div>
  )
}
