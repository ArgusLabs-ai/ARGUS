'use client'

/* Execution graph — the Argus Instrument spec graph (05), driven by a real
   run. Drag nodes, drag the canvas to pan, zoom at the pointer, click a node
   or a satellite to inspect it; selecting dims everything off its path.
   Model and layout live in lib/graph-model.ts; .g* rules in globals.css. */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  Zap, Shuffle, Database, Sparkles, Wrench, ShieldCheck, Send, Circle,
  AlertTriangle, X as XIcon, Info, Clock, Plus, RotateCcw, type LucideIcon,
} from 'lucide-react'
import type { RunRecord } from '@/lib/types'
import { displayTopology } from '@/lib/run-utils'
import { failureChain } from '@/lib/run-detail'
import {
  W, NODE_H, TOOL_GAP_Y, BUS_Y, STATUS_META, EDGE_COLOR,
  layoutGraph, edgeState, related, pathBetween, pillRowWidth,
  type GNode, type GStatus, type NodeKind, type PillTone,
} from '@/lib/graph-model'
import GraphInspector from './GraphInspector'

const KIND_ICON: Record<NodeKind, LucideIcon> = {
  trigger: Zap, transform: Shuffle, retrieval: Database, llm: Sparkles,
  tool: Wrench, guard: ShieldCheck, output: Send, default: Circle,
}
const BADGE: Partial<Record<GStatus, LucideIcon>> = {
  fail: AlertTriangle, crashed: XIcon, semantic: AlertTriangle, degraded: Info,
}
const PILL_ICON: Record<PillTone, LucideIcon> = {
  error: AlertTriangle, slow: Clock, sem: Sparkles, empty: Info, more: Plus,
}

const MIN_Z = 0.3
const MAX_Z = 2.2
const FIT_PAD = 40
const FULL_H = 470
const EMBED_MAX_H = 560

type Sel = { node: string; pill: number | null } | null
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

export default function ExecutionGraph({
  run, onViewFull, onSelectNode, flush = false, selectedNode = null,
}: {
  run: RunRecord
  onViewFull?: () => void
  /** Open the full step detail for a node (the inspector's "Step details"). */
  onSelectNode?: (n: string) => void
  /** Embedded in a scrolling page: wheel-zoom needs ⌘/ctrl, height fits content. */
  flush?: boolean
  selectedNode?: string | null
}) {
  const topo = useMemo(() => displayTopology(run.graph_node_names, run.graph_edge_map), [run])
  const edgeMap = topo.edges
  const initial = useMemo(() => layoutGraph(run, topo.nodes, edgeMap), [run, topo, edgeMap])

  /* The blame path, origin → where it surfaced, through every hop between. */
  const prop = useMemo(() => {
    const chain = failureChain(run)
    if (chain.length < 2) return chain
    const out: string[] = [chain[0]]
    for (let i = 1; i < chain.length; i++) out.push(...pathBetween(edgeMap, chain[i - 1], chain[i]).slice(1))
    return out
  }, [run, edgeMap])

  const [nodes, setNodes] = useState<GNode[]>(initial)
  useEffect(() => setNodes(initial), [initial])
  const byId = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n])), [nodes])

  const [showTools, setShowTools] = useState(true)
  const [view, setView] = useState({ s: 1, x: 0, y: 0 })
  const scale = view.s
  const [height, setHeight] = useState(flush ? 300 : FULL_H)
  const [panning, setPanning] = useState(false)
  const [sel, setSel] = useState<Sel>(null)
  const touched = useRef(false)

  const canvasRef = useRef<HTMLDivElement>(null)
  const nodeEls = useRef(new Map<string, HTMLDivElement>())
  const rowEls = useRef(new Map<string, HTMLDivElement>())

  /* Measured geometry: node heights, satellite centres and row widths,
     relative to the node — positions change on drag, these do not. */
  const [geo, setGeo] = useState<{ h: Record<string, number>; centers: Record<string, number[]>; rowW: Record<string, number> }>(
    { h: {}, centers: {}, rowW: {} },
  )
  useLayoutEffect(() => {
    const h: Record<string, number> = {}
    const centers: Record<string, number[]> = {}
    const rowW: Record<string, number> = {}
    nodeEls.current.forEach((el, id) => { h[id] = el.offsetHeight })
    rowEls.current.forEach((el, id) => {
      rowW[id] = el.offsetWidth
      centers[id] = Array.from(el.children).map((c) => (c as HTMLElement).offsetLeft + (c as HTMLElement).offsetWidth / 2)
    })
    setGeo({ h, centers, rowW })
  }, [initial, showTools])
  const hOf = useCallback((id: string) => geo.h[id] || NODE_H, [geo])

  /* ── framing ── */
  const frame = useCallback(() => {
    const el = canvasRef.current
    if (!el || !nodes.length) return
    const cw = el.clientWidth
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    for (const n of nodes) {
      const tools = showTools && n.pills.length
      const w = Math.max(W, tools ? (geo.rowW[n.id] ?? pillRowWidth(n.pills)) : 0)
      minX = Math.min(minX, n.x)
      minY = Math.min(minY, n.y - (n.isRoot ? 22 : 0))
      maxX = Math.max(maxX, n.x + w)
      maxY = Math.max(maxY, n.y + hOf(n.id) + (tools ? TOOL_GAP_Y + 25 : 0))
    }
    const bw = maxX - minX
    const bh = maxY - minY
    const sx = (cw - FIT_PAD * 2) / bw
    const ch = flush ? Math.round(clamp(Math.min(1, sx) * bh + FIT_PAD * 2, 240, EMBED_MAX_H)) : el.clientHeight
    const s = clamp(Math.min(1, sx, (ch - FIT_PAD * 2) / bh), MIN_Z, 1)
    setHeight(ch)
    setView({ s, x: (cw - bw * s) / 2 - minX * s, y: (ch - bh * s) / 2 - minY * s })
  }, [nodes, showTools, geo, hOf, flush])

  /* Re-frame on content or width change until the user takes the wheel. */
  useEffect(() => { touched.current = false }, [initial])
  useEffect(() => {
    const el = canvasRef.current
    if (!el) return
    if (!touched.current) frame()
    const ro = new ResizeObserver(() => { if (!touched.current) frame() })
    ro.observe(el)
    return () => ro.disconnect()
  }, [frame])

  /* Zoom about a canvas point; `next` maps the current scale to the new one. */
  const zoomAt = useCallback((px: number, py: number, next: (s: number) => number) => {
    touched.current = true
    setView((v) => {
      const z = clamp(next(v.s), MIN_Z, MAX_Z)
      return { s: z, x: px - (px - v.x) * (z / v.s), y: py - (py - v.y) * (z / v.s) }
    })
  }, [])

  useEffect(() => {
    const el = canvasRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      /* Embedded, the page owns scroll; zoom needs a modifier (trackpad
         pinch arrives as ctrl+wheel). */
      if (flush && !(e.ctrlKey || e.metaKey)) return
      e.preventDefault()
      const r = el.getBoundingClientRect()
      zoomAt(e.clientX - r.left, e.clientY - r.top, (s) => s * (e.deltaY > 0 ? 0.9 : 1.1))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [flush, zoomAt])

  /* ── selection ── */
  useEffect(() => { if (selectedNode && byId[selectedNode]) setSel({ node: selectedNode, pill: null }) }, [selectedNode]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!sel) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setSel(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sel])
  const keep = useMemo(() => (sel ? related(sel.node, edgeMap) : null), [sel, edgeMap])
  const selNode = sel ? byId[sel.node] ?? null : null
  const selPill = selNode && sel?.pill != null ? selNode.pills[sel.pill] ?? null : null

  /* ── pointer: pan + drag, both with capture so a release outside the
        canvas still ends the gesture ── */
  const onCanvasDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || (e.target as HTMLElement).closest('.gnode, .gtool, .ginsp')) return
    const sx = e.clientX, sy = e.clientY, ox = view.x, oy = view.y
    const el = e.currentTarget
    el.setPointerCapture(e.pointerId)
    setPanning(true)
    const move = (ev: PointerEvent) => { touched.current = true; setView((v) => ({ ...v, x: ox + ev.clientX - sx, y: oy + ev.clientY - sy })) }
    const up = (ev: PointerEvent) => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      setPanning(false)
      if (Math.abs(ev.clientX - sx) < 3 && Math.abs(ev.clientY - sy) < 3) setSel(null)
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  }

  const onNodeDown = (e: React.PointerEvent<HTMLDivElement>, id: string) => {
    if (e.button !== 0) return
    e.stopPropagation()
    const n = byId[id]
    if (!n) return
    const el = e.currentTarget
    const sx = e.clientX, sy = e.clientY, ox = n.x, oy = n.y
    let moved = false
    el.setPointerCapture(e.pointerId)
    el.classList.add('dragging')
    const move = (ev: PointerEvent) => {
      const dx = (ev.clientX - sx) / scale
      const dy = (ev.clientY - sy) / scale
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true
      if (!moved) return
      touched.current = true
      setNodes((prev) => prev.map((p) => (p.id === id ? { ...p, x: Math.round(ox + dx), y: Math.round(oy + dy) } : p)))
    }
    const up = () => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      el.classList.remove('dragging')
      if (!moved) setSel({ node: id, pill: null })
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  }

  const onNodeKey = (e: React.KeyboardEvent, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel({ node: id, pill: null }); return }
    const d = e.shiftKey ? 24 : 8
    const mv: Record<string, [number, number]> = { ArrowLeft: [-d, 0], ArrowRight: [d, 0], ArrowUp: [0, -d], ArrowDown: [0, d] }
    const m = mv[e.key]
    if (!m) return
    e.preventDefault()
    touched.current = true
    setNodes((prev) => prev.map((p) => (p.id === id ? { ...p, x: p.x + m[0], y: p.y + m[1] } : p)))
  }

  /* ── edges + buses ── */
  const svg = useMemo(() => {
    const edges: { d: string; head: string; color: string; width: number; cls: string }[] = []
    for (const [from, tos] of Object.entries(edgeMap)) {
      for (const to of tos) {
        const a = byId[from]
        const b = byId[to]
        if (!a || !b) continue
        const st = edgeState(a.status, b.status)
        const ai = prop.indexOf(a.id)
        const isProp = ai > -1 && prop[ai + 1] === b.id
        const sx = a.x + W
        const sy = a.y + hOf(a.id) / 2
        const ex = b.x
        const ey = b.y + hOf(b.id) / 2
        /* A loop back to an earlier column swings under both nodes. */
        const back = ex <= sx
        const dx = back ? 90 : Math.max(36, Math.abs(ex - sx) * 0.5)
        const dy = back ? Math.max(hOf(a.id), hOf(b.id)) + 50 : 0
        edges.push({
          d: `M${sx},${sy} C${sx + dx},${sy + dy} ${ex - dx},${ey + dy} ${ex},${ey}`,
          head: `M${ex - 6},${ey - 3.6} L${ex},${ey} L${ex - 6},${ey + 3.6} Z`,
          color: isProp && st === 'pass' ? 'var(--tool)' : EDGE_COLOR[st],
          width: st === 'pass' && !isProp ? 1.2 : st === 'skipped' ? 1.2 : 1.9,
          cls: st === 'running' ? 'e-live' : isProp ? 'e-prop' : '',
        })
      }
    }

    const buses: { d: string; color: string; width: number; dash?: string }[] = []
    if (showTools) {
      for (const n of nodes) {
        const centers = geo.centers[n.id]
        if (!n.pills.length || !centers?.length) continue
        const top = n.y + hOf(n.id)
        const busY = top + BUS_Y
        const rowY = top + TOOL_GAP_Y
        const err = n.pills.some((p) => p.tone === 'error')
        const color = err ? 'var(--tool)' : 'var(--edge-pass)'
        const dash = err ? undefined : '3 3'
        const width = err ? 1.5 : 1
        const mid = n.x + W / 2
        const xs = centers.map((c) => n.x + c)
        buses.push({ d: `M${mid},${top} L${mid},${busY}`, color, width, dash })
        buses.push({ d: `M${Math.min(mid, ...xs)},${busY} L${Math.max(mid, ...xs)},${busY}`, color, width, dash })
        xs.forEach((x, i) => {
          const pe = n.pills[i]?.tone === 'error'
          buses.push({ d: `M${x},${busY} L${x},${rowY}`, color: pe ? 'var(--tool)' : color, width, dash: pe ? undefined : dash })
        })
      }
    }
    return { edges, buses }
  }, [edgeMap, byId, nodes, prop, hOf, geo, showTools])

  const extent = useMemo(() => ({
    w: Math.max(0, ...nodes.map((n) => n.x + Math.max(W, geo.rowW[n.id] ?? 0) + 120)),
    h: Math.max(0, ...nodes.map((n) => n.y + hOf(n.id) + TOOL_GAP_Y + 160)),
  }), [nodes, geo, hOf])

  const present = useMemo(() => {
    const s = new Set<GStatus>(['pass'])
    nodes.forEach((n) => s.add(n.status))
    return (['pass', 'crashed', 'fail', 'semantic', 'degraded', 'running', 'skipped'] as GStatus[]).filter((x) => s.has(x))
  }, [nodes])
  const tones = useMemo(() => new Set(nodes.flatMap((n) => n.pills.map((p) => p.tone))), [nodes])
  const signalCount = nodes.reduce((k, n) => k + n.pills.filter((p) => p.tone !== 'more').length, 0)

  return (
    <div className="gwrap">
      <div className="gbar">
        <span className="gbar-title">Execution graph</span>
        <span className="chip chip-idle gbar-count">{nodes.length} nodes</span>
        <span className="gbar-sp" />
        {signalCount > 0 && (
          <label className="gbar-tgl">
            <span
              className={`switch${showTools ? ' on' : ''}`}
              role="switch"
              aria-checked={showTools}
              tabIndex={0}
              onClick={() => setShowTools((v) => !v)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setShowTools((v) => !v) } }}
            />
            Signals
          </label>
        )}
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => { touched.current = false; setNodes(initial); setSel(null) }}
        >
          <RotateCcw />Re-layout
        </button>
        <div className="gzoom">
          <button type="button" aria-label="Zoom out" onClick={() => { const el = canvasRef.current; if (el) zoomAt(el.clientWidth / 2, el.clientHeight / 2, (s) => s / 1.18) }}>−</button>
          <span className="gzoom-val">{Math.round(scale * 100)}%</span>
          <button type="button" aria-label="Zoom in" onClick={() => { const el = canvasRef.current; if (el) zoomAt(el.clientWidth / 2, el.clientHeight / 2, (s) => s * 1.18) }}>+</button>
          <button type="button" aria-label="Fit to view" className="gzoom-fit" onClick={() => { touched.current = false; frame() }}>FIT</button>
        </div>
        {onViewFull && <button type="button" className="btn btn-sm btn-ghost" onClick={onViewFull}>Full view</button>}
      </div>

      <div
        ref={canvasRef}
        className={`gcanvas${panning ? ' panning' : ''}`}
        /* The inspector needs room; a short embedded canvas grows while it is open. */
        style={{ height: selNode ? Math.max(height, 420) : height }}
        onPointerDown={onCanvasDown}
      >
        <span className="gtick tl" /><span className="gtick tr" /><span className="gtick bl" /><span className="gtick br" />

        <div className="gworld" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${scale})` }}>
          <svg width={extent.w} height={extent.h} className="gedges">
            {svg.edges.map((p, i) => (
              <g key={i}>
                <path d={p.d} fill="none" stroke={p.color} strokeWidth={p.width} className={p.cls} />
                <path d={p.head} fill={p.color} />
              </g>
            ))}
            {svg.buses.map((b, i) => (
              <path key={`b${i}`} d={b.d} fill="none" stroke={b.color} strokeWidth={b.width} strokeDasharray={b.dash} />
            ))}
          </svg>

          {nodes.map((n) => {
            const m = STATUS_META[n.status]
            const Icon = KIND_ICON[n.kind]
            const Badge = BADGE[n.status]
            const dim = keep ? !keep.has(n.id) : false
            return (
              <div key={n.id}>
                <div
                  ref={(el) => { if (el) nodeEls.current.set(n.id, el); else nodeEls.current.delete(n.id) }}
                  className={`gnode ${m.cls}${n.isRoot ? ' rootcause' : ''}${sel?.node === n.id ? ' selected' : ''}${dim ? ' dimmed' : ''}`}
                  style={{ transform: `translate3d(${n.x}px, ${n.y}px, 0)` }}
                  tabIndex={0}
                  role="button"
                  aria-label={`${n.id}, ${m.label}${n.ms != null ? `, ${n.ms} milliseconds` : ''}${n.isRoot ? ', root cause' : ''}`}
                  onPointerDown={(e) => onNodeDown(e, n.id)}
                  onKeyDown={(e) => onNodeKey(e, n.id)}
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
                          : n.status === 'running' ? 'paused'
                          : `${(n.ms ?? 0).toLocaleString()} ms`}
                        {n.pills.length > 0 && <span className="gnode-sig">· {n.pills.reduce((k, p) => k + p.signals.length, 0)} sig</span>}
                      </div>
                    </div>
                  </div>
                  {Badge && <span className="gnode-badge" style={{ background: m.color }}><Badge /></span>}
                </div>

                {showTools && n.pills.length > 0 && (
                  <div
                    ref={(el) => { if (el) rowEls.current.set(n.id, el); else rowEls.current.delete(n.id) }}
                    className={`gtools${dim ? ' dimmed' : ''}`}
                    style={{ transform: `translate3d(${n.x}px, ${n.y + hOf(n.id) + TOOL_GAP_Y}px, 0)` }}
                  >
                    {n.pills.map((p, i) => {
                      const PIcon = PILL_ICON[p.tone]
                      return (
                        <span
                          key={i}
                          className={`gtool t-${p.tone}${sel?.node === n.id && sel.pill === i ? ' selected' : ''}`}
                          role="button"
                          tabIndex={0}
                          title={p.field || undefined}
                          aria-label={`${p.field || p.id}, ${p.tag}`}
                          onPointerDown={(e) => e.stopPropagation()}
                          onClick={(e) => { e.stopPropagation(); setSel({ node: n.id, pill: i }) }}
                          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel({ node: n.id, pill: i }) } }}
                        >
                          <PIcon className="gtool-ico" />
                          {p.id}
                          {p.tone !== 'more' && <span className="gtool-kind">·{p.tag}</span>}
                        </span>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {selNode && (
          <GraphInspector
            run={run}
            node={selNode}
            pill={selPill}
            prop={prop}
            onClose={() => setSel(null)}
            onOpenDetails={onSelectNode ? () => onSelectNode(selNode.id) : undefined}
          />
        )}
      </div>

      <div className="glegend">
        {present.map((s) => (
          <span key={s} className="glegend-i"><span className={`lg-key lg-${s}`} />{STATUS_META[s].label}</span>
        ))}
        {tones.has('error') && <span className="glegend-i"><span className="lg-pill lg-t-error" />signal · critical</span>}
        {tones.has('sem') && <span className="glegend-i"><span className="lg-pill lg-t-sem" />signal · semantic</span>}
        {tones.has('slow') && <span className="glegend-i"><span className="lg-pill lg-t-slow" />signal · warning</span>}
        {tones.has('empty') && <span className="glegend-i"><span className="lg-pill lg-t-empty" />signal · coherence</span>}
        <span className="glegend-hint" title={`Drag nodes · drag canvas to pan · ${flush ? '⌘ + scroll' : 'scroll'} to zoom`}>{flush ? '⌘ + scroll to zoom' : 'Scroll to zoom'}</span>
      </div>
    </div>
  )
}
