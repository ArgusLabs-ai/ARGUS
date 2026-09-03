'use client'

import { useRef, useState, type MouseEvent, type WheelEvent } from 'react'
import type { RunRecord, StepStatus } from '@/lib/types'
import { formatDur, fmtCost } from '@/lib/run-utils'

const SENTINEL_NODES = new Set(['__start__', '__end__', 'START', 'END'])

function displayName(name: string): string {
  return name.length > 12 ? `${name.slice(0, 11)}...` : name
}

function dagLayers(nodes: string[], edgeMap: Record<string, string[]>): string[][] {
  const nodeSet = new Set(nodes)
  const indegree = new Map(nodes.map((n) => [n, 0]))
  const outgoing = new Map<string, string[]>()
  let usableEdgeCount = 0

  for (const node of nodes) outgoing.set(node, [])
  for (const [source, targets] of Object.entries(edgeMap ?? {})) {
    if (!nodeSet.has(source)) continue
    for (const target of targets ?? []) {
      if (!nodeSet.has(target)) continue
      usableEdgeCount += 1
      outgoing.get(source)?.push(target)
      indegree.set(target, (indegree.get(target) ?? 0) + 1)
    }
  }

  if (usableEdgeCount === 0) return nodes.map((n) => [n])

  const layers: string[][] = []
  let ready = nodes.filter((n) => (indegree.get(n) ?? 0) === 0)
  const seen = new Set<string>()

  while (ready.length > 0) {
    const layer = ready.filter((n) => !seen.has(n))
    if (layer.length === 0) break
    layers.push(layer)
    for (const node of layer) seen.add(node)

    const next: string[] = []
    for (const node of layer) {
      for (const target of outgoing.get(node) ?? []) {
        indegree.set(target, (indegree.get(target) ?? 0) - 1)
        if ((indegree.get(target) ?? 0) === 0) next.push(target)
      }
    }
    ready = nodes.filter((n) => next.includes(n))
  }

  const leftovers = nodes.filter((n) => !seen.has(n))
  if (leftovers.length) {
    // Cycles or partial edge metadata: fall back gracefully instead of hiding nodes.
    layers.push(...leftovers.map((n) => [n]))
  }

  return layers.length ? layers : nodes.map((n) => [n])
}

function statusVisual(status: StepStatus | undefined, degradedDownstream: boolean) {
  if (!status) {
    return {
      color: 'var(--ink-4)',
      border: 'var(--line-2)',
      bg: 'var(--overlay)',
      soft: 'var(--fill-subtle)',
      icon: null as 'check' | 'warn' | 'down' | 'x' | null,
      dashed: true,
    }
  }
  if (status === 'pass' && !degradedDownstream) {
    return { color: 'var(--ok)', border: 'color-mix(in srgb, var(--ok) 45%, transparent)', bg: 'var(--ok-dim)', soft: 'var(--ok-dim)', icon: 'check' as const, dashed: false }
  }
  if (status === 'crashed') {
    return { color: 'var(--tool)', border: 'color-mix(in srgb, var(--tool) 45%, transparent)', bg: 'var(--tool-dim)', soft: 'var(--tool-dim)', icon: 'x' as const, dashed: false }
  }
  if (status === 'fail') {
    return { color: 'var(--quality)', border: 'color-mix(in srgb, var(--quality) 45%, transparent)', bg: 'var(--quality-dim)', soft: 'var(--quality-dim)', icon: 'warn' as const, dashed: false }
  }
  if (status === 'semantic_fail') {
    return { color: 'var(--semantic)', border: 'color-mix(in srgb, var(--semantic) 45%, transparent)', bg: 'var(--semantic-dim)', soft: 'var(--semantic-dim)', icon: 'warn' as const, dashed: false }
  }
  if (status === 'degraded_input') {
    return { color: 'var(--quality)', border: 'color-mix(in srgb, var(--quality) 45%, transparent)', bg: 'var(--quality-dim)', soft: 'var(--quality-dim)', icon: 'warn' as const, dashed: false }
  }
  if (status === 'interrupted') {
    return { color: 'var(--quality)', border: 'color-mix(in srgb, var(--quality) 45%, transparent)', bg: 'var(--quality-dim)', soft: 'var(--quality-dim)', icon: 'warn' as const, dashed: false }
  }
  if (status === 'skipped' || status === 'retried') {
    return { color: 'var(--ink-4)', border: 'var(--line-2)', bg: 'var(--overlay)', soft: 'var(--fill-subtle)', icon: null, dashed: true }
  }
  return { color: 'var(--quality)', border: 'color-mix(in srgb, var(--quality) 45%, transparent)', bg: 'var(--quality-dim)', soft: 'var(--quality-dim)', icon: 'down' as const, dashed: true }
}

function StatusIcon({ icon, color }: { icon: ReturnType<typeof statusVisual>['icon']; color: string }) {
  if (icon === 'check') {
    return <path d="M4 7.2l2.1 2.1L10.5 4.8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
  }
  if (icon === 'warn') {
    return (
      <>
        <path d="M7 2.5l5.2 9H1.8l5.2-9Z" stroke="currentColor" strokeWidth="1.35" strokeLinejoin="round" />
        <path d="M7 5.4v2.6" stroke="currentColor" strokeWidth="1.35" strokeLinecap="round" />
        <circle cx="7" cy="9.7" r="0.55" fill={color} />
      </>
    )
  }
  if (icon === 'x') {
    return <path d="M4.5 4.5l5 5M9.5 4.5l-5 5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
  }
  if (icon === 'down') {
    return <path d="M7 3.5v6M4.4 7.4L7 10l2.6-2.6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
  }
  return null
}

export default function PipelineOverview({ run, onViewFull }: { run: RunRecord; onViewFull: () => void }) {
  const [viewport, setViewport] = useState({ x: 0, y: 0, scale: 0.82 })
  const [isDragging, setIsDragging] = useState(false)
  const dragRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null)
  const nodes = (run.graph_node_names ?? []).filter((n) => !n.startsWith('__') && !SENTINEL_NODES.has(n))
  const layers = dagLayers(nodes, run.graph_edge_map ?? {})
  const stepMap = new Map((run.steps ?? []).map((s) => [s.node_name, s]))
  const firstFailureIndex = run.first_failure_step
    ? (run.steps ?? []).findIndex((s) => s.node_name === run.first_failure_step)
    : -1

  const visualFor = (name: string) => {
    const step = stepMap.get(name)
    const degradedDownstream = Boolean(step && firstFailureIndex >= 0 && step.step_index > firstFailureIndex && run.overall_status !== 'clean')
    return statusVisual(step?.status, degradedDownstream)
  }

  const layerVisual = (layer: string[]) => {
    const targets = layer.map(visualFor)
    const crashed = targets.find((v) => v.color === 'var(--tool)')
    if (crashed) return crashed
    const silentFailure = targets.find((v) => v.color === 'var(--quality)')
    if (silentFailure) return silentFailure
    const semanticFailure = targets.find((v) => v.color === 'var(--semantic)')
    if (semanticFailure) return semanticFailure
    const degraded = targets.find((v) => v.color === 'var(--quality)')
    if (degraded) return degraded
    const pass = targets.find((v) => v.color === 'var(--ok)')
    return pass ?? targets[0]
  }

  const handleWheel = (event: WheelEvent<HTMLDivElement>) => {
    event.preventDefault()
    setViewport((current) => ({
      ...current,
      scale: Math.min(1.35, Math.max(0.5, current.scale - event.deltaY * 0.001)),
    }))
  }

  const handleMouseDown = (event: MouseEvent<HTMLDivElement>) => {
    setIsDragging(true)
    dragRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX: viewport.x,
      originY: viewport.y,
    }
  }

  const handleMouseMove = (event: MouseEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag) return
    setViewport((current) => ({
      ...current,
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY,
    }))
  }

  const stopDragging = () => {
    dragRef.current = null
    setIsDragging(false)
  }

  const zoomBy = (delta: number) => {
    setViewport((current) => ({
      ...current,
      scale: Math.min(1.35, Math.max(0.5, current.scale + delta)),
    }))
  }

  return (
    <div className="card rounded-xl p-3.5 min-h-[130px] max-h-[190px] overflow-clip">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          <h3 className="text-[13px] font-bold tracking-[-0.01em]" style={{ color: 'var(--text-primary)' }}>Pipeline Overview</h3>
          <div className="flex items-center overflow-hidden rounded-md" style={{ border: '1px solid var(--border-subtle)' }}>
            <button
              type="button"
              onClick={() => zoomBy(-0.1)}
              className="w-6 h-6 text-[13px] font-bold leading-none transition-colors hover:bg-white/5"
              style={{ color: 'var(--text-muted)' }}
              aria-label="Zoom out pipeline"
            >
              -
            </button>
            <button
              type="button"
              onClick={() => zoomBy(0.1)}
              className="w-6 h-6 text-[13px] font-bold leading-none transition-colors hover:bg-white/5"
              style={{ color: 'var(--text-muted)', borderLeft: '1px solid var(--border-subtle)' }}
              aria-label="Zoom in pipeline"
            >
              +
            </button>
          </div>
        </div>
        <button
          onClick={onViewFull}
          className="text-[11px] font-semibold flex items-center gap-1 px-2 py-1 rounded-md transition-colors"
          style={{ color: 'var(--text-muted)', border: '1px solid var(--border-subtle)' }}
        >
          Full Graph
          <svg width="10" height="10" viewBox="0 0 13 13" fill="none">
            <path d="M4.2 3.2h5.6v5.6M9.7 3.3L3.4 9.6" stroke="currentColor" strokeWidth="1.45" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>
      </div>

      <div
        className="relative h-[122px] overflow-hidden rounded-[12px] select-none"
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={stopDragging}
        onMouseLeave={stopDragging}
        style={{
          background: 'var(--overlay)',
          cursor: isDragging ? 'grabbing' : 'grab',
        }}
      >
        <div
          className="absolute left-0 top-1/2 flex items-center py-5 min-w-max"
          style={{
            transform: `translate(${viewport.x + 10}px, calc(-50% + ${viewport.y}px)) scale(${viewport.scale})`,
            transformOrigin: 'left center',
            transition: isDragging ? 'none' : 'transform 120ms ease-out',
          }}
        >
          {layers.map((layer, layerIndex) => {
            const sourceConnector = layerIndex < layers.length - 1 ? layerVisual(layer) : null
            const targetConnector = layerIndex < layers.length - 1 ? layerVisual(layers[layerIndex + 1]) : null
            const priorCount = layers.slice(0, layerIndex).reduce((sum, l) => sum + l.length, 0)
            return (
              <div key={layer.join('|')} className="flex items-center shrink-0">
                <div className="flex flex-col gap-5">
                  {layer.map((name, rowIndex) => {
                    const visual = visualFor(name)
                    const step = stepMap.get(name)
                    const displayIndex = priorCount + rowIndex + 1
                    return (
                      <div key={name} className="relative w-[92px] h-[70px] rounded-[15px] flex flex-col items-center justify-center gap-1.5"
                        style={{
                          background: visual.bg,
                          border: `2px solid ${visual.border}`,
                          color: visual.color,
                          boxShadow: `0 8px 20px ${visual.soft}`,
                        }}
                      >
                        <span
                          className="absolute -top-3 left-1/2 -translate-x-1/2 w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-extrabold tabular-nums"
                          style={{ background: visual.color, color: 'var(--on-accent)', boxShadow: `0 4px 10px ${visual.soft}` }}
                        >
                          {displayIndex}
                        </span>
                        <span className="text-[13px] font-extrabold tracking-[-0.03em] leading-none text-center px-2" style={{ color: 'var(--text-primary)' }} title={name}>
                          {displayName(name)}
                        </span>
                        <span className="text-[12px] font-bold tabular-nums" style={{ color: 'var(--text-secondary)' }}>
                          {step ? formatDur(step.duration_ms) : '—'}
                        </span>
                        {step?.llm_usage?.total_cost_usd != null && step.llm_usage.total_cost_usd > 0 && (
                          <span className="text-[10px] font-bold tabular-nums" style={{ color: 'var(--ok)' }}>
                            {fmtCost(step.llm_usage.total_cost_usd)}
                          </span>
                        )}
                        {visual.icon && (
                          <span
                            className="absolute -bottom-2 -right-2 w-7 h-7 rounded-full flex items-center justify-center"
                            style={{ background: 'var(--panel)', color: visual.color, boxShadow: 'var(--shadow-md)' }}
                          >
                            <svg width="15" height="15" viewBox="0 0 14 14" fill="none">
                              <StatusIcon icon={visual.icon} color={visual.color} />
                            </svg>
                          </span>
                        )}
                      </div>
                    )
                  })}
                </div>
                {sourceConnector && targetConnector && (
                  <div className="relative w-[34px] h-[70px] shrink-0 -mx-[1px]">
                    <div
                      className="absolute left-0 right-1/2 top-1/2 -translate-y-1/2 border-t-[3px]"
                      style={{
                        borderColor: sourceConnector.color,
                        borderStyle: sourceConnector.dashed ? 'dashed' : 'solid',
                      }}
                    />
                    <div
                      className="absolute left-1/2 right-0 top-1/2 -translate-y-1/2 border-t-[3px]"
                      style={{
                        borderColor: targetConnector.color,
                        borderStyle: targetConnector.dashed ? 'dashed' : 'solid',
                      }}
                    />
                    <span
                      className="absolute left-1/2 top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full"
                      style={{
                        background: targetConnector.color,
                        boxShadow: `0 0 0 3px var(--panel), 0 4px 10px ${targetConnector.soft}`,
                      }}
                    />
                    <span
                      className="absolute right-0 top-1/2 -translate-y-1/2 w-2.5 h-2.5 rotate-45"
                      style={{
                        borderTop: `3px solid ${targetConnector.color}`,
                        borderRight: `3px solid ${targetConnector.color}`,
                      }}
                    />
                    {layer.length > 1 || layers[layerIndex + 1].length > 1 ? (
                      <div
                        className="absolute left-1/2 top-2 bottom-2 border-l-2"
                        style={{ borderColor: targetConnector.color, borderStyle: targetConnector.dashed ? 'dashed' : 'solid', opacity: 0.35 }}
                      />
                    ) : null}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
