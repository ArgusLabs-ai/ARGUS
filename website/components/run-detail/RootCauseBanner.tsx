'use client'

import type { Finding } from '@/lib/types'

export default function RootCauseBanner({
  chain,
  finding,
  onSelectNode,
}: {
  chain?: string[]
  finding?: Finding | null
  onSelectNode?: (node: string) => void
}) {
  if (finding) {
    return (
      <button
        type="button"
        onClick={() => finding.node && onSelectNode?.(finding.node)}
        className="w-full rounded-[var(--r-panel)] px-4 py-3 text-left"
        style={{
          backgroundColor: 'color-mix(in srgb, var(--tool) 6%, transparent)',
          border: '1px solid color-mix(in srgb, var(--tool) 22%, transparent)',
        }}
      >
        <p className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--tool)]">
          Root cause
        </p>
        <p className="mt-1 text-[13px] leading-snug text-[var(--ink)]">{finding.reason}</p>
        <p className="mt-1.5 font-mono text-[11px] text-[var(--ink-3)]">
          {finding.origin_node && finding.origin_node !== finding.node
            ? `${finding.origin_node} → ${finding.node}`
            : finding.node}
        </p>
      </button>
    )
  }

  if (!chain || chain.length === 0) return null

  return (
    <div
      className="rounded-[var(--r-panel)] px-4 py-3"
      style={{
        backgroundColor: 'color-mix(in srgb, var(--tool) 6%, transparent)',
        border: '1px solid color-mix(in srgb, var(--tool) 22%, transparent)',
      }}
    >
      <p className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--tool)]">
        Root cause
      </p>
      <p className="mt-1 font-mono text-[13px] text-[var(--ink)]">{chain.join('  →  ')}</p>
    </div>
  )
}
