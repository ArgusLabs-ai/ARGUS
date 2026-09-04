export interface HotspotCell {
  origin: string
  node: string
  count: number
  run_ids: string[]
}

export interface HotspotMatrixData {
  origins: string[]
  nodes: string[]
  cells: HotspotCell[]
  run_count: number
}

interface FindingLike {
  node?: string
  origin_node?: string | null
  suppressed?: boolean
}

interface RunLike {
  run_id?: string
  findings?: FindingLike[]
}

export function aggregateHotspots(runs: RunLike[]): HotspotMatrixData {
  const cells = new Map<string, HotspotCell>()
  for (const run of runs) {
    for (const finding of run.findings ?? []) {
      if (finding.suppressed || !finding.node) continue
      const origin = finding.origin_node || finding.node
      const key = `${origin}\0${finding.node}`
      const slot = cells.get(key) ?? {
        origin,
        node: finding.node,
        count: 0,
        run_ids: [],
      }
      slot.count += 1
      if (run.run_id && !slot.run_ids.includes(run.run_id)) {
        slot.run_ids.push(run.run_id)
      }
      cells.set(key, slot)
    }
  }
  const list = Array.from(cells.values())
  return {
    origins: Array.from(new Set(list.map((c) => c.origin))).sort(),
    nodes: Array.from(new Set(list.map((c) => c.node))).sort(),
    cells: list.sort((a, b) => a.origin.localeCompare(b.origin) || a.node.localeCompare(b.node)),
    run_count: runs.length,
  }
}
