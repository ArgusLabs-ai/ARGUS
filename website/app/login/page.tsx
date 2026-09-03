'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { useAuth } from '@/lib/auth'

export default function LoginPage() {
  const { user, loading } = useAuth()
  const router = useRouter()

  useEffect(() => {
    if (!loading && user) {
      router.replace('/')
    }
  }, [loading, user, router])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <span className="text-sm" style={{ color: 'var(--text-secondary)' }}>
          Loading...
        </span>
      </div>
    )
  }

  if (user) return null

  return (
    <div className="flex items-center justify-center min-h-[60vh] overflow-auto h-full px-8 py-10">
      <div
        className="flex flex-col items-center gap-8 p-10 rounded-xl"
        style={{
          background: 'var(--bg-surface)',
          border: '1px solid var(--border-default)',
          boxShadow: '0 8px 32px var(--hover)',
          minWidth: '360px',
        }}
      >
        {/* Logo */}
        <div className="flex flex-col items-center gap-2">
          <svg
            width="36"
            height="36"
            viewBox="0 0 18 18"
            fill="none"
            xmlns="http://www.w3.org/2000/svg"
          >
            <path
              d="M9 1.5L16.5 5.5V12.5L9 16.5L1.5 12.5V5.5L9 1.5Z"
              stroke="var(--iris)"
              strokeWidth="1.2"
              fill="none"
            />
            <circle
              cx="9"
              cy="9"
              r="2.2"
              fill="var(--iris)"
              fillOpacity="0.3"
              stroke="var(--iris)"
              strokeWidth="1.1"
            />
            <circle cx="9" cy="9" r="0.9" fill="var(--iris)" />
          </svg>
          <h1
            className="text-lg font-semibold tracking-tight"
            style={{ color: 'var(--text-primary)' }}
          >
            ARGUS
          </h1>
          <p
            className="text-xs"
            style={{ color: 'var(--text-secondary)' }}
          >
            Not authenticated
          </p>
        </div>

        {/* CLI instruction */}
        <div className="flex flex-col items-center gap-3 w-full">
          <p className="text-xs text-center" style={{ color: 'var(--text-secondary)' }}>
            Run the following command in your terminal to log in:
          </p>
          <div
            className="w-full rounded-lg px-4 py-3 flex items-center justify-center"
            style={{
              background: 'var(--bg-base, var(--panel))',
              border: '1px solid var(--border-default)',
            }}
          >
            <span
              className="font-mono text-sm"
              style={{ color: 'var(--iris)' }}
            >
              argus login
            </span>
          </div>
          <p
            className="text-[10px] text-center max-w-[260px]"
            style={{ color: 'var(--text-faint)' }}
          >
            After logging in, refresh this page.
          </p>
        </div>
      </div>
    </div>
  )
}
