'use client'

import { useEffect, useState, useCallback } from 'react'
import type { Candidate, Signature, SignatureStatsData, Tab } from './types'
import { CandidateCard, SignatureRow, SharedSignatureRow } from './ApprovalRows'

// ── Main Page ────────────────────────────────────────────────

export default function ApprovalsPage() {
  const [tab, setTab] = useState<Tab>('pending')
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [privateSigs, setPrivateSigs] = useState<Signature[]>([])
  const [sharedSigs, setSharedSigs] = useState<Signature[]>([])
  const [sigStats, setSigStats] = useState<Record<string, SignatureStatsData>>({})
  const [loading, setLoading] = useState(true)
  const [acting, setActing] = useState<string | null>(null)
  const [actingBool, setActingBool] = useState(false)
  const [syncing, setSyncing] = useState(false)

  const fetchData = useCallback(async () => {
    try {
      const [candRes, privRes, sharedRes, statsRes] = await Promise.all([
        fetch('/api/candidates'),
        fetch('/api/custom-signatures'),
        fetch('/api/shared-signatures'),
        fetch('/api/signature-stats'),
      ])
      if (candRes.ok) {
        const data = await candRes.json()
        setCandidates(data.candidates || [])
      }
      if (privRes.ok) {
        const data = await privRes.json()
        setPrivateSigs((data || []).map((s: Signature) => ({ ...s, source: s.source || 'learned' })))
      }
      if (sharedRes.ok) {
        const data = await sharedRes.json()
        setSharedSigs((data || []).map((s: Signature) => ({ ...s, source: 'shared' })))
      }
      if (statsRes.ok) {
        const data: SignatureStatsData[] = await statsRes.json()
        const map: Record<string, SignatureStatsData> = {}
        for (const s of data) {
          map[s.sig_id] = s
        }
        setSigStats(map)
      }
    } catch {
      // server not running
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  async function handleApprovePrivate(id: string) {
    setActing(id)
    try {
      const res = await fetch(`/api/candidates/${id}/approve`, { method: 'POST' })
      if (res.ok) await fetchData()
    } catch { /* ignore */ }
    setActing(null)
  }

  async function handleApproveShared(id: string) {
    setActing(id)
    try {
      const res = await fetch(`/api/candidates/${id}/approve-shared`, { method: 'POST' })
      if (res.ok) {
        await fetchData()
      } else {
        const data = await res.json().catch(() => ({}))
        alert(data.error || 'Failed — are you logged in? Run: argus login')
      }
    } catch { /* ignore */ }
    setActing(null)
  }

  async function handleReject(id: string) {
    setActing(id)
    try {
      const res = await fetch(`/api/candidates/${id}/reject`, { method: 'POST' })
      if (res.ok) await fetchData()
    } catch { /* ignore */ }
    setActing(null)
  }

  async function handleRemovePrivate(id: string) {
    setActingBool(true)
    try {
      const res = await fetch(`/api/custom-signatures/${id}`, { method: 'DELETE' })
      if (res.ok) await fetchData()
    } catch { /* ignore */ }
    setActingBool(false)
  }

  async function handleToggleDisable(id: string, currentlyDisabled: boolean) {
    setActingBool(true)
    try {
      const action = currentlyDisabled ? 'enable' : 'disable'
      const res = await fetch(`/api/custom-signatures/${id}/${action}`, { method: 'POST' })
      if (res.ok) await fetchData()
    } catch { /* ignore */ }
    setActingBool(false)
  }

  async function handleSync() {
    setSyncing(true)
    try {
      const res = await fetch('/api/shared-signatures/sync')
      if (res.ok) await fetchData()
    } catch { /* ignore */ }
    setSyncing(false)
  }

  return (
    <div className="max-w-3xl mx-auto px-8 py-10 overflow-auto h-full">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-3 mb-1">
          <h1 className="text-[22px] font-bold tracking-tight" style={{ color: 'var(--text-primary)' }}>
            Approvals
          </h1>
          {candidates.length > 0 && (
            <span
              className="text-[11px] font-bold px-2 py-0.5 rounded-full"
              style={{ background: 'var(--tool-dim)', color: 'var(--tool)' }}
            >
              {candidates.length} pending
            </span>
          )}
        </div>
        <p className="text-[13px]" style={{ color: 'var(--text-secondary)' }}>
          Review AI-discovered patterns and manage your active detection library.
        </p>
      </div>

      {/* Tabs */}
      <div className="flex items-center justify-between mb-5">
        <div
          className="flex rounded-lg overflow-hidden"
          style={{ border: '1px solid var(--border-subtle)', width: 'fit-content' }}
        >
          {([
            { key: 'pending' as Tab, label: 'Pending', count: candidates.length },
            { key: 'private' as Tab, label: 'Private', count: privateSigs.length },
            { key: 'shared' as Tab, label: 'Shared', count: sharedSigs.length },
          ]).map(({ key, label, count }) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className="px-4 py-2 text-[13px] font-medium transition-all flex items-center gap-1.5"
              style={{
                background: tab === key ? 'var(--iris-dim)' : 'transparent',
                color: tab === key ? 'var(--iris)' : 'var(--text-muted)',
              }}
            >
              {label}
              <span
                className="text-[10.5px] font-bold px-1.5 py-0.5 rounded-full min-w-[20px] text-center"
                style={{
                  background: tab === key ? 'var(--iris-dim)' : 'var(--bg-elevated)',
                  color: tab === key ? 'var(--iris)' : 'var(--text-muted)',
                }}
              >
                {count}
              </span>
            </button>
          ))}
        </div>

        {tab === 'shared' && (
          <button
            type="button"
            onClick={handleSync}
            disabled={syncing}
            className="px-3.5 py-1.5 rounded-lg text-[12px] font-medium transition-all flex items-center gap-1.5"
            style={{
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border-subtle)',
              color: 'var(--text-secondary)',
              opacity: syncing ? 0.5 : 1,
            }}
          >
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M2.5 8a5.5 5.5 0 019.3-4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
              <path d="M13.5 8a5.5 5.5 0 01-9.3 4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
              <path d="M11 3l1 1.5 1.5-1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M5 13l-1-1.5-1.5 1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            {syncing ? 'Syncing...' : 'Sync'}
          </button>
        )}
      </div>

      {/* Content */}
      {loading ? (
        <div className="text-center py-16 text-[13px]" style={{ color: 'var(--text-muted)' }}>
          Loading...
        </div>
      ) : tab === 'pending' ? (
        candidates.length === 0 ? (
          <div
            className="text-center py-16 rounded-xl"
            style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}
          >
            <p className="text-[14px] font-medium mb-1" style={{ color: 'var(--text-secondary)' }}>
              No pending approvals
            </p>
            <p className="text-[12px]" style={{ color: 'var(--text-muted)' }}>
              When the LLM investigator discovers new failure patterns, they appear here for your review.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {candidates.map((c) => (
              <CandidateCard
                key={c.id}
                candidate={c}
                onApprovePrivate={handleApprovePrivate}
                onApproveShared={handleApproveShared}
                onReject={handleReject}
                acting={acting}
              />
            ))}
          </div>
        )
      ) : tab === 'private' ? (
        privateSigs.length === 0 ? (
          <div
            className="text-center py-16 rounded-xl"
            style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}
          >
            <p className="text-[14px] font-medium mb-1" style={{ color: 'var(--text-secondary)' }}>
              No private patterns
            </p>
            <p className="text-[12px]" style={{ color: 'var(--text-muted)' }}>
              Approve pending patterns as &quot;Private&quot; to add them to your local detection engine.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-2.5">
            {privateSigs.map((s) => (
              <SignatureRow key={s.id} sig={s} stats={sigStats[s.id] || null} onRemove={handleRemovePrivate} onToggleDisable={handleToggleDisable} acting={actingBool} />
            ))}
          </div>
        )
      ) : (
        sharedSigs.length === 0 ? (
          <div
            className="text-center py-16 rounded-xl"
            style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}
          >
            <p className="text-[14px] font-medium mb-1" style={{ color: 'var(--text-secondary)' }}>
              No shared patterns
            </p>
            <p className="text-[12px]" style={{ color: 'var(--text-muted)' }}>
              Approve pending patterns as &quot;Shared&quot; to contribute to the community, or click Sync to pull existing ones.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {sharedSigs.map((s) => (
              <SharedSignatureRow key={s.id} sig={s} stats={sigStats[s.id] || null} />
            ))}
          </div>
        )
      )}
    </div>
  )
}
