'use client'

/* Execution graph — a direct port of the Argus Instrument spec graph.
   Drag nodes, pan the canvas, wheel-zoom, click a node to inspect it.
   Geometry (node width, tool-row offset, bezier control points, arrowheads)
   matches the spec exactly; see globals.css for the .g* rules. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Zap, Shuffle, Database, Sparkles, Wrench, ShieldCheck, FileOutput, Circle,
  AlertTriangle, X as XIcon, Info, Check, Clock, Plug, Maximize2, type LucideIcon,
} from 'lucide-react'
import type { RunRecord, StepStatus } from '@/lib/types'
import { getFailureMeta } from '@/lib/failure-labels'

const W = 178          // node width — spec
const NODE_H = 52      // node box height used for edge anchoring — spec
const TOOL_GAP_Y = 30  // node bottom → tool row — spec
const GAP_X = 96
const GAP_Y = 132
const PAD = 48

/* ── kinds ───────────────────────────────────────────────────── */

type NodeKind = 'trigger' | 'transform' | 'retrieval' | 'llm' | 'tool' | 'guard' | 'output' | 'default'

const KIND_ICON: Record<NodeKind, LucideIcon> = {
  trigger: Zap, transform: Shuffle, retrieval: Database, llm: Sparkles,
  tool: Wrench, guard: ShieldCheck, output: FileOutput, default: Circle,
}

function inferKind(name: string): NodeKind {
  const n = name.toLowerCase()
  if (/(ingest|start|trigger|input|entry)/.test(n)) return 'trigger'
  if (/(fetch|retriev|search|query|load|source)/.test(n)) return 'retrieval'
  if (/(summar|synth|generat|plan|llm|model|revise|draft|answer)/.test(n)) return 'llm'
  if (/(verify|validat|check|guard|review)/.test(n)) return 'guard'
  if (/(final|output|report|emit|render)/.test(n)) return 'output'
  if (/(merge|map|transform|parse|format|normal)/.test(n)) return 'transform'
  if (/(tool|call|api|http)/.test(n)) return 'tool'
  return 'default'
}

/* ── status ──────────────────────────────────────────────────── */

type S = 'pass' | 'fail' | 'crashed' | 'semantic' | 'degraded' | 'running' | 'skipped'

const STATUS_META: Record<S, { cls: string; label: string; color: string; badge: LucideIcon | null }> = {
  pass:     { cls: 's-pass',     label: 'pass',           color: 'var(--ok)',          badge: null },
  fail:     { cls: 's-fail',     label: 'silent failure', color: 'var(--quality)',     badge: AlertTriangle },
  crashed:  { cls: 's-crashed',  label: 'crashed',        color: 'var(--tool)',        badge: XIcon },
  semantic: { cls: 's-semantic', label: 'semantic fail',  color: 'var(--semantic)',    badge: AlertTriangle },
  degraded: { cls: 's-degraded', label: 'degraded input', color: 'var(--coherence)',   badge: Info },
  running:  { cls: 's-running',  label: 'running',        color: 'var(--iris-bright)', badge: null },
  skipped:  { cls: 's-skipped',  label: 'not reached',    color: 'var(--ink-3)',       badge: null },
}

const SEVERITY: Partial<Record<S, number>> = { crashed: 4, semantic: 3, fail: 2, degraded: 1 }

const EDGE_COLOR: Record<S, string> = {
  pass: 'var(--edge-pass)', running: 'var(--iris)', skipped: 'var(--edge-skip)',
  crashed: 'var(--tool)', semantic: 'var(--semantic)', fail: 'var(--quality)',
  degraded: 'var(--coherence)',
}

function mapStatus(s: StepStatus | undefined): S {
  switch (s) {
    case 'pass': return 'pass'
    case 'crashed': return 'crashed'
    case 'semantic_fail': return 'semantic'
    case 'degraded_input': return 'degraded'
    case 'fail': case 'retried': return 'fail'
    case 'skipped': case undefined: return 'skipped'
    default: return 'pass'
  }
}

/* ── tool chips, derived from real per-node findings ──────────── */

type ToolStatus = 'ok' | 'error' | 'slow' | 'empty' | 'skipped'
const TOOL_META: Record<ToolStatus, { cls: string; icon: LucideIcon }> = {
  ok:      { cls: 't-ok',      icon: Check },
  error:   { cls: 't-error',   icon: AlertTriangle },
  slow:    { cls: 't-slow',    icon: Clock },
  empty:   { cls: 't-empty',   icon: Info },
  skipped: { cls: 't-skipped', icon: Plug },
}

interface Tool { id: string; tag: string; status: ToolStatus }

function toolsFor(run: RunRecord, node: string): Tool[] {
  const step = (run.steps ?? []).find((s) => s.node_name === node)
  if (!step) return []
  const insp = step.inspection
  const out: Tool[] = []

  for (const tf of insp?.tool_failures ?? []) {
    const meta = getFailureMeta(tf.failure_type)
    const status: ToolStatus =
      tf.failure_type.includes('empty') ? 'empty'
      : /slow|timeout|latency|fast/.test(tf.failure_type) ? 'slow'
      : tf.severity === 'critical' ? 'error' : 'slow'
    out.push({ id: tf.field_name || meta.label, tag: meta.label, status })
  }
  for (const sig of insp?.semantic_signals ?? []) {
    out.push({
      id: sig.field_path?.join('.') || 'output',
      tag: sig.sig_id,
      status: sig.severity === 'critical' ? 'error' : 'slow',
    })
  }
  for (const an of step.anomaly_signals ?? []) {
    out.push({ id: an.field_path || 'behaviour', tag: an.anomaly_id, status: an.severity === 'critical' ? 'error' : 'slow' })
  }
  return out.slice(0, 4)
}

/* ── layout ──────────────────────────────────────────────────── */

function dagLayers(names: string[], edgeMap: Record<string, string[]>): string[][] {
  const indeg: Record<string, number> = {}
  names.forEach((n) => { indeg[n] = 0 })
  for (const [, tos] of Object.entries(edgeMap ?? {})) {
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

interface GNode {
  id: string; kind: NodeKind; status: S; ms: number | null
  x: number; y: number; isRoot: boolean; tools: Tool[]
}

/* ── component ───────────────────────────────────────────────── */

export default function ExecutionGraph({
  run, onViewFull, onSelectNode, flush = false, selectedNode = null,
}: {
  run: RunRecord
  onViewFull?: () => void
  onSelectNode?: (n: string) => void
  /** Flush mode: no toolbar, hairline top/bottom, sized to its content —
      the overview idiom. The default is the full canvas with controls. */
  flush?: boolean
  selectedNode?: string | null
}) {
  const names = useMemo(() => run.graph_node_names ?? [], [run])
  const edgeMap = useMemo(() => run.graph_edge_map ?? {}, [run])
  const chain = useMemo(() => run.root_cause_chain ?? [], [run])

  const initial = useMemo<GNode[]>(() => {
    const layers = dagLayers(names, edgeMap)
    const widest = Math.max(1, ...layers.map((l) => l.length))
    const yCentre = ((widest - 1) * GAP_Y) / 2
    /* One crown: the origin. The rest of the chain reads from the red edge. */
    const rootSet = new Set((run.root_cause_chain ?? []).slice(0, 1))
    const stepFor = (n: string) => (run.steps ?? []).find((s) => s.node_name === n)
    const out: GNode[] = []
    layers.forEach((layer, col) => {
      layer.forEach((id, row) => {
        const st = stepFor(id)
        out.push({
          id,
          kind: inferKind(id),
          status: mapStatus(st?.status),
          ms: st ? Math.round(st.duration_ms) : null,
          x: PAD + col * (W + GAP_X),
          y: PAD + row * GAP_Y - ((layer.length - 1) * GAP_Y) / 2 + (flush ? yCentre : 150),
          isRoot: rootSet.has(id),
          tools: toolsFor(run, id),
        })
      })
    })
    return out
  }, [run, names, edgeMap, flush])

  const [nodes, setNodes] = useState<GNode[]>(initial)
  useEffect(() => setNodes(initial), [initial])

  const [scale, setScale] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [selected, setSelected] = useState<string | null>(null)
  useEffect(() => { if (flush) setSelected(selectedNode) }, [flush, selectedNode])
  const [panning, setPanning] = useState(false)
  const canvasRef = useRef<HTMLDivElement>(null)
  const drag = useRef<{ id: string | null; ox: number; oy: number } | null>(null)

  const byId = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n])), [nodes])

  /* pan */
  const onCanvasDown = useCallback((e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest('.gnode')) return
    setPanning(true)
    const sx = e.clientX - pan.x
    const sy = e.clientY - pan.y
    const move = (ev: PointerEvent) => setPan({ x: ev.clientX - sx, y: ev.clientY - sy })
    const up = () => {
      setPanning(false)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }, [pan])

  /* node drag */
  const onNodeDown = useCallback((e: React.PointerEvent, id: string) => {
    e.stopPropagation()
    const n = byId[id]
    if (!n) return
    drag.current = { id, ox: e.clientX / scale - n.x, oy: e.clientY / scale - n.y }
    const move = (ev: PointerEvent) => {
      const d = drag.current
      if (!d?.id) return
      setNodes((prev) => prev.map((p) =>
        p.id === d.id ? { ...p, x: ev.clientX / scale - d.ox, y: ev.clientY / scale - d.oy } : p))
    }
    const up = () => {
      drag.current = null
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }, [byId, scale])

  /* wheel zoom */
  useEffect(() => {
    const el = canvasRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      /* In the flush overview the page owns scroll; zoom needs a modifier. */
      if (flush && !(e.ctrlKey || e.metaKey)) return
      e.preventDefault()
      setScale((s) => Math.min(2, Math.max(0.35, s - e.deltaY * 0.0015)))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [flush])

  function nudge(e: React.KeyboardEvent, id: string) {
    const d = e.shiftKey ? 24 : 8
    const delta: Record<string, [number, number]> = {
      ArrowLeft: [-d, 0], ArrowRight: [d, 0], ArrowUp: [0, -d], ArrowDown: [0, d],
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault(); setSelected(id); onSelectNode?.(id); return
    }
    const mv = delta[e.key]
    if (!mv) return
    e.preventDefault()
    setNodes((prev) => prev.map((p) => (p.id === id ? { ...p, x: p.x + mv[0], y: p.y + mv[1] } : p)))
  }

  /* edges */
  const edgeState = (a: GNode, b: GNode): S => {
    if (a.status === 'running' || b.status === 'running') return 'running'
    if (a.status === 'skipped' && b.status === 'skipped') return 'skipped'
    const sa = SEVERITY[a.status] ?? 0
    const sb = SEVERITY[b.status] ?? 0
    if (sa === 0 && sb === 0) return 'pass'
    return sa >= sb ? a.status : b.status
  }

  const paths = useMemo(() => {
    const out: { d: string; head: string; color: string; width: number; cls: string }[] = []
    for (const [from, tos] of Object.entries(edgeMap)) {
      for (const to of tos) {
        const a = byId[from]
        const b = byId[to]
        if (!a || !b) continue
        const st = edgeState(a, b)
        const ai = chain.indexOf(a.id)
        const bi = chain.indexOf(b.id)
        const isProp = ai > -1 && bi === ai + 1
        const sx = a.x + W
        const sy = a.y + NODE_H / 2
        const ex = b.x
        const ey = b.y + NODE_H / 2
        const dx = Math.max(36, Math.abs(ex - sx) * 0.5)
        out.push({
          d: `M${sx},${sy} C${sx + dx},${sy} ${ex - dx},${ey} ${ex},${ey}`,
          head: `M${ex - 6},${ey - 3.6} L${ex},${ey} L${ex - 6},${ey + 3.6} Z`,
          color: EDGE_COLOR[st],
          width: st === 'pass' || st === 'skipped' ? 1.2 : 1.9,
          cls: st === 'running' ? 'e-live' : isProp ? 'e-prop' : '',
        })
      }
    }
    return out
  }, [byId, edgeMap, chain])

  const extent = useMemo(() => ({
    w: Math.max(900, ...nodes.map((n) => n.x + W + 80)),
    h: Math.max(470, ...nodes.map((n) => n.y + NODE_H + TOOL_GAP_Y + 90)),
  }), [nodes])
  const flushNatural = useMemo(() => {
    const anyTools = initial.some((n) => n.tools.length > 0)
    const bottom = Math.max(0, ...initial.map((n) => n.y + NODE_H + (n.tools.length ? TOOL_GAP_Y + 26 : 0)))
    const right = Math.max(0, ...initial.map((n) => n.x + W)) + PAD
    return { h: Math.max(190, bottom + (anyTools ? 28 : 40)), w: right }
  }, [initial])

  /* Flush graphs fit the workspace width: wide pipelines scale down rather
     than run off the right edge, and the canvas height follows the scale. */
  const [fit, setFit] = useState(1)
  useEffect(() => {
    if (!flush) return
    const el = canvasRef.current
    if (!el) return
    const measure = () => {
      const avail = el.clientWidth
      if (!avail) return
      const s = Math.min(1, Math.max(0.45, avail / flushNatural.w))
      setFit(s)
      setScale(s)
      setPan({ x: Math.max(0, (avail - flushNatural.w * s) / 2), y: 0 })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [flush, flushNatural])
  const flushH = Math.max(150, Math.round(flushNatural.h * fit))

  const sel = selected ? byId[selected] : null

  const canvas = (
      <div
        ref={canvasRef}
        className={`gcanvas${panning ? ' panning' : ''}${flush ? ' flush' : ''}`}
        onPointerDown={onCanvasDown}
        style={flush ? { height: flushH } : undefined}
      >
        {!flush && (<><span className="gtick tl" /><span className="gtick tr" />
        <span className="gtick bl" /><span className="gtick br" /></>)}

        <div
          style={{
            position: 'absolute', top: 0, left: 0, transformOrigin: '0 0',
            transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${scale})`,
            willChange: 'transform',
          }}
        >
          <svg
            width={extent.w}
            height={extent.h}
            style={{ position: 'absolute', top: 0, left: 0, overflow: 'visible', pointerEvents: 'none' }}
          >
            {paths.map((p, i) => (
              <g key={i}>
                <path d={p.d} fill="none" stroke={p.color} strokeWidth={p.width} className={p.cls} />
                <path d={p.head} fill={p.color} />
              </g>
            ))}
          </svg>

          {nodes.map((n) => {
            const m = STATUS_META[n.status]
            const Icon = KIND_ICON[n.kind]
            const Badge = m.badge
            return (
              <div key={n.id}>
                <div
                  className={`gnode ${m.cls}${n.isRoot ? ' rootcause' : ''}`}
                  style={{ transform: `translate3d(${n.x}px, ${n.y}px, 0)` }}
                  tabIndex={0}
                  role="button"
                  aria-label={`${n.id}, ${m.label}${n.ms != null ? `, ${n.ms} milliseconds` : ''}${n.isRoot ? ', root cause' : ''}`}
                  onPointerDown={(e) => onNodeDown(e, n.id)}
                  onClick={() => { setSelected(n.id); onSelectNode?.(n.id) }}
                  onKeyDown={(e) => nudge(e, n.id)}
                >
                  {n.isRoot && <span className="gnode-tab">ROOT CAUSE</span>}
                  <div className="gnode-top">
                    <span className="gnode-ico"><Icon /></span>
                    <div style={{ minWidth: 0 }}>
                      <div className="gnode-name">{n.id}</div>
                      <div className="gnode-sub">
                        <span style={{ color: m.color }}>●</span>
                        {n.status === 'skipped' ? 'not reached'
                          : n.status === 'crashed' ? 'raised'
                          : `${(n.ms ?? 0).toLocaleString()} ms`}
                        {n.tools.length > 0 && ` · ${n.tools.length}T`}
                      </div>
                    </div>
                  </div>
                  {Badge && (
                    <span className="gnode-badge" style={{ background: m.color }}>
                      <Badge style={{ width: 9, height: 9 }} />
                    </span>
                  )}
                </div>

                {n.tools.length > 0 && (
                  <div
                    className="gtools"
                    style={{ transform: `translate3d(${n.x}px, ${n.y + NODE_H + TOOL_GAP_Y}px, 0)` }}
                  >
                    {n.tools.map((t, i) => {
                      const tm = TOOL_META[t.status]
                      const TIcon = tm.icon
                      return (
                        <span key={i} className={`gtool ${tm.cls}`} title={`${t.id} — ${t.tag}`}>
                          <TIcon className="gtool-ico" />
                          {t.id}
                          <span className="gtool-kind">·{t.tag}</span>
                        </span>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {sel && !flush && (
          <div className="ginsp">
            <div className="ginsp-h">
              <span className="gnode-name">{sel.id}</span>
              <span style={{ color: STATUS_META[sel.status].color, fontSize: 11 }}>
                {STATUS_META[sel.status].label}
              </span>
              <button type="button" className="ginsp-x" aria-label="Close inspector" onClick={() => setSelected(null)}>×</button>
            </div>
            <div className="ginsp-b">
              <div className="ginsp-sec">
                <div className="kv-row"><span className="kv-k">kind</span><span className="kv-v">{sel.kind}</span></div>
                <div className="kv-row"><span className="kv-k">duration</span><span className="kv-v">{sel.ms ?? '—'} ms</span></div>
                <div className="kv-row"><span className="kv-k">root cause</span><span className="kv-v">{sel.isRoot ? 'yes' : 'no'}</span></div>
                <div className="kv-row"><span className="kv-k">signals</span><span className="kv-v">{sel.tools.length}</span></div>
              </div>
              {sel.tools.map((t, i) => (
                <div key={i} className="ginsp-sec">
                  <div className="kv-row"><span className="kv-k">{t.id}</span><span className="kv-v">{t.tag}</span></div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
  )

  if (flush) return <div className="flushcanvas graph">{canvas}</div>

  return (
    <div className="gwrap">
      <div className="gbar">
        <h2 className="text-[13px] font-semibold" style={{ color: 'var(--ink)' }}>Execution Graph</h2>
        <span className="chip chip-idle !h-[20px] !px-[7px] font-mono !text-[11px]">{nodes.length} nodes</span>
        <span className="gbar-sp" />
        <div className="glegend">
          {(['pass', 'crashed', 'fail', 'semantic', 'degraded', 'skipped'] as S[]).map((s) => (
            <span key={s} className="glegend-i">
              <i style={{ background: STATUS_META[s].color }} />
              {STATUS_META[s].label}
            </span>
          ))}
        </div>
        <div className="gzoom">
          <button type="button" aria-label="Zoom out" onClick={() => setScale((s) => Math.max(0.35, s - 0.15))}>−</button>
          <span className="gzoom-val">{Math.round(scale * 100)}%</span>
          <button type="button" aria-label="Zoom in" onClick={() => setScale((s) => Math.min(2, s + 0.15))}>+</button>
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-sm btn-icon"
          aria-label="Reset view"
          onClick={() => { setScale(1); setPan({ x: 0, y: 0 }); setNodes(initial) }}
        >
          <Maximize2 />
        </button>
        {onViewFull && (
          <button type="button" className="btn btn-sm btn-ghost" onClick={onViewFull}>Full view</button>
        )}
      </div>

      {canvas}
    </div>
  )
}
