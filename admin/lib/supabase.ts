import { createClient } from '@supabase/supabase-js'

// The telemetry project (supabase/telemetry/). The publishable key is public by
// design: it can read and write nothing on its own (verified: every table and
// function refuses anon). Data is visible only to a signed-in user listed in
// admin_users, enforced by row-level security, not by this app.
const URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? 'https://hpzvhxodoqlsvbodpxcr.supabase.co'
const KEY =
  process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? 'sb_publishable_q4eqcDWRIGK6iHxqawe-fQ_NoUkO8OQ'

export const supabase = createClient(URL, KEY)

export type Role = 'owner' | 'maintainer'

export type Card = {
  id: string
  received_at: string
  install: string
  run: string
  fingerprint: string
  status: string
  argus_version: string
  langgraph_version: string | null
  python: string
  ci: boolean
  n_steps: number
  n_findings: number
  card: CardBody
}

export type Shape = {
  t: string
  v?: boolean
  len?: number
  f?: string[]
  item?: Shape
  keys?: Record<string, Shape>
  more?: number
}

export type CardBody = {
  blamed: string[]
  graph: { nodes: number; edges: number; cyclic: boolean }
  coverage?: Record<string, number>
  duration_ms?: number | null
  steps: {
    node: string
    status: string
    attempt: number
    ms?: number
    error: boolean
    update?: Shape | null
    tools: { name: string; error: boolean; output?: Shape }[]
    review: { role: string; kind: string }[]
  }[]
  findings: {
    type: string
    severity: string
    source: string
    node: string
    origin?: string | null
    field?: string | null
    heuristic: boolean
    confidence?: number | null
    suppressed: boolean
  }[]
}
