import type { NodeEvent, RunRecord } from './types'

/* ── Colours ──────────────────────────────────────────────────────
   Tokens, not literals: these feed `style={{ color }}` on components that
   render in both themes, so a fixed dark-palette grey washes out on light.
   The CLI's own colours live in Python; these mirror its *semantics*. */

export const C_GREEN = 'var(--ok)'
export const C_AMBER = 'var(--quality)'
export const C_RED = 'var(--tool)'
export const C_MAGENTA = 'var(--semantic)'

/** A translucent wash of any colour. Hex-alpha concatenation (`${c}10`) emits
    `var(--tool)10` — invalid CSS that silently drops the rule — so every tint
    goes through `color-mix`, which works on tokens and literals alike. */
export function tint(color: string, percent: number): string {
  return `color-mix(in srgb, ${color} ${percent}%, transparent)`
}

export const STATUS_DOT: Record<string, { dot: string; color: string }> = {
  clean: { dot: '\u25CF', color: C_GREEN },
  silent_failure: { dot: '\u25CF', color: C_AMBER },
  crashed: { dot: '\u25CF', color: C_RED },
  semantic_fail: { dot: '\u25CF', color: C_MAGENTA },
  interrupted: { dot: '\u23F8', color: C_AMBER },
}

export const STATUS_LABEL_STYLE: Record<string, string> = {
  clean: 'text-emerald-600 font-bold',
  silent_failure: 'text-amber-600 font-bold',
  crashed: 'text-red-600 font-bold',
  semantic_fail: 'text-purple-600 font-bold',
  interrupted: 'text-amber-600 font-bold',
}

export const SENTINEL_NODES = new Set(['__start__', '__end__', 'START', 'END'])

/** Real graph nodes. `graph_node_names` is the raw `graph.nodes` key list, so it
    carries LangGraph's `__start__` / `__end__` sentinels — they never run and
    must never render as steps. Every node-list consumer goes through here. */
export function displayNodes(names: string[] | null | undefined): string[] {
  return (names ?? []).filter((n) => !SENTINEL_NODES.has(n) && !n.startsWith('__'))
}

/** Node list plus edge map with the sentinels dropped from both sides, so
    in-degree / root detection is not skewed by a `__start__ → first` edge. */
export function displayTopology(
  names: string[] | null | undefined,
  edgeMap: Record<string, string[]> | null | undefined,
): { nodes: string[]; edges: Record<string, string[]> } {
  const nodes = displayNodes(names)
  const keep = new Set(nodes)
  const edges: Record<string, string[]> = {}
  for (const [src, dests] of Object.entries(edgeMap ?? {})) {
    if (!keep.has(src)) continue
    edges[src] = (dests ?? []).filter((d) => keep.has(d))
  }
  return { nodes, edges }
}

/* ── Helpers ─────────────────────────────────────────────────────── */

export function formatDur(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '\u2014'
  return `${Math.round(ms)} ms`
}

export function formatTimestamp(iso: string): string {
  return iso.slice(0, 16).replace('T', '  ')
}

export function fmtCost(usd: number): string {
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(2)}`
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return `${n}`
}

/* ── Step display ────────────────────────────────────────────────── */

export interface StepDisplay {
  icon: string
  iconColor: string
  label: string
  labelColor: string
  warnSuffix?: boolean
}

export function getStepDisplay(event: NodeEvent): StepDisplay {
  const insp = event.inspection
  const hasWarnings =
    event.status === 'pass' &&
    insp !== null &&
    (
      (insp.empty_fields?.length ?? 0) > 0 ||
      (insp.type_mismatches?.length ?? 0) > 0 ||
      (insp.unannotated_successors?.length ?? 0) > 0 ||
      (insp.suspicious_empty_keys?.length ?? 0) > 0 ||
      ((insp.tool_failures?.length ?? 0) > 0 && !insp.has_tool_failure)
    )

  if (event.status === 'pass' && hasWarnings) {
    return { icon: '~', iconColor: C_AMBER, label: 'pass', labelColor: C_GREEN, warnSuffix: true }
  }
  if (event.status === 'pass') {
    return { icon: '\u2713', iconColor: C_GREEN, label: 'pass', labelColor: C_GREEN }
  }
  if (event.status === 'degraded_input') {
    return { icon: '\u2B07', iconColor: C_AMBER, label: 'degraded input', labelColor: C_AMBER }
  }
  if (event.status === 'fail') {
    return { icon: '\u26A0', iconColor: C_AMBER, label: 'silent failure', labelColor: C_AMBER }
  }
  if (event.status === 'semantic_fail') {
    return { icon: '\u2297', iconColor: C_MAGENTA, label: 'semantic fail', labelColor: C_MAGENTA }
  }
  if (event.status === 'interrupted') {
    return { icon: '\u23F8', iconColor: C_AMBER, label: 'interrupted', labelColor: C_AMBER }
  }
  if (event.status === 'retried') {
    return { icon: '\u21BB', iconColor: 'var(--ink-3)', label: 'retried', labelColor: 'var(--ink-3)' }
  }
  if (event.status === 'skipped') {
    return { icon: '\u25CB', iconColor: 'var(--ink-3)', label: 'skipped', labelColor: 'var(--ink-3)' }
  }
  return { icon: '\u2717', iconColor: C_RED, label: 'crashed', labelColor: C_RED }
}

export function successorName(event: NodeEvent, run: RunRecord): string {
  const succs = run.graph_edge_map?.[event.node_name] ?? []
  return succs[0] ?? 'next node'
}

/* ── Detail lines ────────────────────────────────────────────────── */

export interface DetailLine {
  text: string
  color?: string
  italic?: boolean
  underline?: boolean
  bold?: boolean
  indent?: boolean
}

export function dl(text: string, opts: Omit<DetailLine, 'text'> = {}): DetailLine {
  return { text, ...opts }
}

export function getDetailLines(event: NodeEvent, run: RunRecord): DetailLine[] {
  const lines: DetailLine[] = []
  const insp = event.inspection
  const display = getStepDisplay(event)

  if (event.status === 'fail' && insp) {
    if (insp.is_silent_failure) {
      lines.push(dl('context error', { color: C_AMBER, underline: true }))
    }
    if (insp.has_tool_failure) {
      lines.push(dl('tool failure', { color: C_AMBER, underline: true }))
    }
    if (event.semantic_check && !event.semantic_check.passed) {
      lines.push(dl(`semantic judge: ${event.semantic_check.reason}`, { color: C_MAGENTA, italic: true }))
    }
  }

  if (event.status === 'interrupted') {
    lines.push(dl('execution paused \u2014 awaiting human approval', { color: 'var(--ink-3)', italic: true }))
    return lines
  }

  if (event.status === 'semantic_fail') {
    for (const vr of event.validator_results) {
      if (!vr.is_valid) {
        lines.push(dl(`\u2297 ${vr.validator_name}  ${vr.message}`, { color: C_MAGENTA }))
      }
    }
    if (event.anomaly_signals?.length) {
      for (const a of event.anomaly_signals) {
        lines.push(dl(`[${a.anomaly_id}] ${a.reason} \u2014 expected: ${a.expected_behavior}, observed: ${a.observed_behavior}`, { color: 'var(--ink-4)', italic: true }))
      }
    }
    return lines
  }

  if (event.status === 'degraded_input' && insp) {
    const upstream = insp.degraded_upstream_node ?? 'upstream'
    for (const field of insp.degraded_fields ?? []) {
      lines.push(dl(`Field "${field}" missing from input`, { color: 'var(--ink-2)', italic: true }))
    }
    lines.push(dl(`upstream node ${upstream} failed to produce it`, { color: 'var(--ink-3)', italic: true }))
    return lines
  }

  if (event.status === 'pass' && !display.warnSuffix) {
    const passing = event.validator_results.filter((v) => v.is_valid)
    for (const vr of passing) {
      lines.push(dl(`\u2713 ${vr.validator_name}`, { color: 'var(--ok)' }))
    }
    return lines
  }

  if (event.status === 'pass' && display.warnSuffix) {
    const successor = successorName(event, run)
    if (insp?.empty_fields) {
      for (const field of insp.empty_fields) {
        lines.push(dl(`Field "${field}" is empty`, { color: 'var(--ink-3)' }))
      }
      lines.push(dl(`${successor} may receive degraded state`, { color: 'var(--ink-3)' }))
    }
    if (insp?.type_mismatches) {
      for (const m of insp.type_mismatches) {
        lines.push(dl(`Field "${m.field_name}" expected ${m.expected_type}, got ${m.actual_type}`, { color: 'var(--ink-3)' }))
      }
    }
    if (insp?.unannotated_successors?.length) {
      const names = insp.unannotated_successors.join(', ')
      lines.push(dl(`silent-failure detection skipped \u2014 add type hints to: ${names}`, { color: 'var(--ink-3)' }))
    }
    if (insp?.suspicious_empty_keys) {
      for (const key of insp.suspicious_empty_keys) {
        lines.push(dl(`Output key "${key}" is empty (may degrade downstream)`, { color: 'var(--ink-3)' }))
      }
    }
    if (insp?.tool_failures) {
      for (const tf of insp.tool_failures) {
        const tfIcon = tf.severity === 'critical' ? '\u26A0' : '~'
        lines.push(dl(`${tfIcon} Tool ${tf.failure_type}: field "${tf.field_name}" \u2014 ${tf.evidence}`, { color: tf.severity === 'critical' ? C_RED : C_AMBER }))
      }
    }
    if (insp?.semantic_signals?.length) {
      for (const sig of insp.semantic_signals) {
        const path = sig.field_path.join('.')
        lines.push(dl(`[${sig.sig_id}] ${sig.category}  ${path}`, { color: 'var(--ink-4)', italic: true }))
      }
    }
    if (event.anomaly_signals?.length) {
      for (const a of event.anomaly_signals) {
        lines.push(dl(`[${a.anomaly_id}] ${a.reason} \u2014 expected: ${a.expected_behavior}, observed: ${a.observed_behavior}`, { color: 'var(--ink-4)', italic: true }))
      }
    }
    return lines
  }

  const successor = successorName(event, run)
  const isDownstream =
    event.status === 'fail' &&
    run.first_failure_step !== null &&
    event.node_name !== run.first_failure_step

  if (event.exception) {
    lines.push(dl('exception', { color: 'var(--ink-3)' }))
    const firstLine = event.exception.split('\n').find((l) => l.trim()) ?? ''
    lines.push(dl(firstLine, { color: 'var(--ink-2)', italic: true, indent: true }))

    const locMatch = event.exception.match(/File ".*?([^/\\]+\.py)", line (\d+)/)
    if (locMatch) {
      const codeLines = event.exception.split('\n')
      const fileIdx = codeLines.findIndex((l) => l.includes(locMatch[0]))
      const codeLine = fileIdx >= 0 && fileIdx + 1 < codeLines.length ? codeLines[fileIdx + 1].trim() : ''
      if (codeLine) {
        lines.push(dl(`at ${locMatch[1]}:${locMatch[2]}  \u2192  ${codeLine}`, { color: 'var(--ink-3)', italic: true, indent: true }))
      }
    }

    if (event.semantic_check && !event.semantic_check.passed) {
      lines.push(dl(`semantic judge: ${event.semantic_check.reason}`, { color: C_MAGENTA, italic: true, indent: true }))
    }
  }

  if (insp?.tool_failures?.length) {
    lines.push(dl('tool failures', { color: 'var(--ink-3)' }))
    for (const tf of insp.tool_failures) {
      const tfIcon = tf.severity === 'critical' ? '\u26A0' : '~'
      lines.push(dl(`${tfIcon} Tool ${tf.failure_type}: field "${tf.field_name}" \u2014 ${tf.evidence}`, { color: tf.severity === 'critical' ? C_RED : C_AMBER, indent: true }))
    }
  }

  if (insp?.semantic_signals?.length) {
    for (const sig of insp.semantic_signals) {
      const path = sig.field_path.join('.')
      lines.push(dl(`[${sig.sig_id}] ${sig.category}  ${path}`, { color: 'var(--ink-4)', italic: true, indent: true }))
    }
  }

  if (event.anomaly_signals?.length) {
    for (const a of event.anomaly_signals) {
      lines.push(dl(`[${a.anomaly_id}] ${a.reason} \u2014 expected: ${a.expected_behavior}, observed: ${a.observed_behavior}`, { color: 'var(--ink-4)', italic: true, indent: true }))
    }
  }

  if (insp) {
    if (insp.missing_fields?.length) {
      lines.push(dl('missing fields', { color: 'var(--ink-3)' }))
      for (const field of insp.missing_fields) {
        lines.push(dl(`Field "${field}" is missing`, { color: 'var(--ink-2)', italic: true, indent: true }))
      }
      lines.push(dl(`${successor} received bad state`, { color: 'var(--ink-3)', italic: true, indent: true }))
    } else if (insp.empty_fields?.length) {
      lines.push(dl('missing fields', { color: 'var(--ink-3)' }))
      for (const field of insp.empty_fields) {
        lines.push(dl(`Field "${field}" is empty`, { color: 'var(--ink-2)', italic: true, indent: true }))
      }
      lines.push(dl(`${successor} received bad state`, { color: 'var(--ink-3)', italic: true, indent: true }))
    } else if (insp.type_mismatches?.length) {
      lines.push(dl('missing fields', { color: 'var(--ink-3)' }))
      for (const m of insp.type_mismatches) {
        lines.push(dl(`Field "${m.field_name}" expected ${m.expected_type}, got ${m.actual_type}`, { color: 'var(--ink-2)', italic: true, indent: true }))
      }
    }
  }

  if (isDownstream && run.first_failure_step) {
    lines.push(dl(`Root cause: ${run.first_failure_step}`, { color: C_RED, bold: true }))
  }

  return lines
}
