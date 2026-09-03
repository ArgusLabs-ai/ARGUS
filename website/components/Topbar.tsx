'use client'

import { useRef, useEffect } from 'react'
import { usePathname } from 'next/navigation'

function getPageTitle(pathname: string): string {
  if (pathname === '/') return 'Runs'
  if (pathname === '/compare') return 'Compare'
  if (pathname === '/guide') return 'Guide'
  if (pathname.startsWith('/runs/')) return 'Run Detail'
  return 'ARGUS'
}

export default function Topbar({ force = false }: { force?: boolean }) {
  const pathname = usePathname()
  const title = getPageTitle(pathname)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
      }
      if (e.key === 'Escape') {
        inputRef.current?.blur()
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [])

  if (pathname === '/' && !force) return null

  return (
    <header
      className="h-11 flex items-center justify-between px-6 shrink-0 gap-4"
      style={{
        borderBottom: '1px solid var(--border-subtle)',
        background: 'var(--overlay)',
        backdropFilter: 'blur(12px)',
      }}
    >
      {/* Left: page title */}
      <span className="text-[12px] font-bold tracking-wide shrink-0" style={{ color: 'var(--text-secondary)' }}>
        {title}
      </span>

      {/* Center: search bar */}
      <div
        className="flex-1 max-w-[480px] flex items-center gap-2.5 px-3 py-1.5 rounded-lg transition-colors"
        style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)' }}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="shrink-0">
          <circle cx="6" cy="6" r="4.5" stroke="var(--ink-4)" strokeWidth="1.2"/>
          <path d="M9.5 9.5L12.5 12.5" stroke="var(--ink-4)" strokeWidth="1.2" strokeLinecap="round"/>
        </svg>
        <input
          ref={inputRef}
          type="text"
          placeholder="Search runs, graphs, nodes..."
          className="flex-1 text-[12px] font-medium bg-transparent outline-none"
          style={{ color: 'var(--text-primary)' }}
        />
        <kbd
          className="text-[10px] font-mono px-1.5 py-0.5 rounded shrink-0"
          style={{ background: 'var(--bg-surface)', color: 'var(--text-muted)', border: '1px solid var(--border-subtle)' }}
        >
          {'\u2318'}K
        </kbd>
      </div>

      {/* Right: indicators */}
      <div className="flex items-center gap-3 shrink-0">
        <button
          className="p-1.5 rounded-md transition-colors hover:bg-white/5"
          style={{ color: 'var(--text-muted)' }}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <rect x="1" y="1" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.1"/>
            <rect x="8" y="1" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.1"/>
            <rect x="1" y="8" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.1"/>
            <rect x="8" y="8" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.1"/>
          </svg>
        </button>
        <span
          className="flex items-center gap-1.5 text-[10px] font-mono px-2 py-1 rounded-md tracking-wide"
          style={{ color: 'var(--text-muted)', background: 'var(--bg-elevated)' }}
        >
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: 'var(--ok)' }} />
          local
        </span>
      </div>
    </header>
  )
}
