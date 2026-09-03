// Shared shapes for the approvals views.

export interface Candidate {
  id: string
  pattern: string
  match_strategy: string
  proposed_category: string
  severity: string
  description: string
  evidence: string[]
  confidence: number
  reasoning: string
  source_run_ids: string[]
  source_nodes: string[]
  times_seen: number
  first_seen: string
  last_seen: string
  status: string
}

export interface Signature {
  id: string
  category: string
  pattern: string
  match_strategy: string
  severity: string
  description: string
  source: string
  metadata: {
    confidence: number | null
    frequency: number | null
    approval_status: string
    approved_at?: string
    contributed_by?: string
    framework_specific: string | null
    disabled?: boolean
    total_hits?: number
    last_hit_at?: string
  }
}

export interface SignatureStatsData {
  sig_id: string
  source: string
  description: string
  total_hits: number
  runs_hit: number
  nodes_hit: number
  first_hit: string
  last_hit: string
  hit_nodes: string[]
  false_positive_count: number
  disabled: boolean
}

export type Tab = 'pending' | 'private' | 'shared'
