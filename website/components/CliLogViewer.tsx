'use client'

import { useState } from 'react'

interface CliLogViewerProps {
  log: string
  runId: string
}

type LogLevel = 'INFO' | 'WARN' | 'WARNING' | 'ERROR' | 'DEBUG' | 'FATAL'

function parseLine(line: string): { timestamp: string; level: LogLevel | null; rest: string } | null {
  if (!line.trim()) return null

  // Match: 2026-04-22T11:50:00.012Z  INFO  ...
  const m = line.match(/^(\S+)\s{2,}(INFO|WARN|WARNING|ERROR|DEBUG|FATAL)\s{2,}(.*)$/)
  if (m) {
    return { timestamp: m[1], level: m[2] as LogLevel, rest: m[3] }
  }
  return { timestamp: '', level: null, rest: line }
}

function levelColor(level: LogLevel | null): string {
  switch (level) {
    case 'INFO':    return 'var(--success)'
    case 'DEBUG':   return 'var(--text-secondary)'
    case 'WARN':
    case 'WARNING': return 'var(--warning)'
    case 'ERROR':   return 'var(--failure)'
    case 'FATAL':   return 'var(--failure)'
    default:        return 'var(--text-tertiary)'
  }
}

function levelBg(level: LogLevel | null): string {
  switch (level) {
    case 'WARN':
    case 'WARNING': return 'var(--quality-dim)'
    case 'ERROR':   return 'var(--tool-dim)'
    case 'FATAL':   return 'var(--tool-dim)'
    default:        return 'transparent'
  }
}

function colorizeRest(rest: string): React.ReactNode[] {
  // Highlight key=value pairs and quoted strings
  const parts: React.ReactNode[] = []
  const regex = /(\w[\w.]*=(?:"[^"]*"|[^\s]*))|("([^"]*)")/g
  let last = 0
  let match: RegExpExecArray | null

  while ((match = regex.exec(rest)) !== null) {
    if (match.index > last) {
      parts.push(<span key={last} style={{ color: 'var(--ink-2)' }}>{rest.slice(last, match.index)}</span>)
    }
    const full = match[0]
    if (match[1]) {
      // key=value
      const eq = full.indexOf('=')
      const key = full.slice(0, eq)
      const val = full.slice(eq + 1)
      parts.push(
        <span key={match.index}>
          <span style={{ color: 'var(--iris-bright)' }}>{key}</span>
          <span style={{ color: 'var(--text-tertiary)' }}>=</span>
          <span style={{ color: 'var(--ok)' }}>{val}</span>
        </span>
      )
    } else if (match[2]) {
      // quoted string
      parts.push(<span key={match.index} style={{ color: 'var(--ink)' }}>{full}</span>)
    }
    last = match.index + full.length
  }

  if (last < rest.length) {
    parts.push(<span key={last} style={{ color: 'var(--ink-2)' }}>{rest.slice(last)}</span>)
  }

  return parts
}

export default function CliLogViewer({ log, runId }: CliLogViewerProps) {
  const [collapsed, setCollapsed] = useState(false)

  const lines = log.split('\n').filter((l) => l.trim())
  const parsed = lines.map((l, i) => ({ raw: l, parsed: parseLine(l), i }))

  const hasErrors = parsed.some((p) => p.parsed?.level === 'ERROR' || p.parsed?.level === 'FATAL')
  const hasWarns = parsed.some((p) => p.parsed?.level === 'WARN' || p.parsed?.level === 'WARNING')

  return (
    <div
      className="rounded-xl border border-border overflow-hidden"
      style={{
        background: 'var(--code-bg)',
      }}
    >
      {/* Terminal titlebar */}
      <div
        className="flex items-center justify-between px-4 py-2.5 border-b border-border"
        style={{ background: 'var(--code-header)' }}
      >
        <div className="flex items-center gap-3">
          {/* Traffic lights */}
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: 'var(--tool)' }} />
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: 'var(--quality)' }} />
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: 'var(--ok)' }} />
          </div>
          <span className="text-xs font-mono text-muted-foreground">{runId}.log</span>
          <div className="flex items-center gap-1.5 ml-1">
            {hasErrors && (
              <span className="text-[10px] px-1.5 py-0.5 rounded font-mono" style={{ color: 'var(--failure)', background: 'color-mix(in srgb, var(--failure) 10%, transparent)', border: '1px solid color-mix(in srgb, var(--failure) 20%, transparent)' }}>
                {parsed.filter(p => p.parsed?.level === 'ERROR' || p.parsed?.level === 'FATAL').length} err
              </span>
            )}
            {hasWarns && (
              <span className="text-[10px] px-1.5 py-0.5 rounded font-mono" style={{ color: 'var(--warning)', background: 'color-mix(in srgb, var(--warning) 8%, transparent)', border: '1px solid color-mix(in srgb, var(--warning) 20%, transparent)' }}>
                {parsed.filter(p => p.parsed?.level === 'WARN' || p.parsed?.level === 'WARNING').length} warn
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          aria-expanded={!collapsed}
          onClick={() => setCollapsed(!collapsed)}
          className="text-[10px] text-muted-foreground hover:text-foreground transition-colors font-mono"
        >
          {collapsed ? 'expand' : 'collapse'}
        </button>
      </div>

      {/* Log lines */}
      {!collapsed && (
        <div
          className="px-0 py-2 overflow-x-auto scrollbar-thin"
          style={{ background: 'var(--code-bg)' }}
        >
          <div className="font-mono text-xs leading-6 min-w-0">
            {parsed.map(({ raw, parsed: p, i }) => {
              if (!p) return null
              const bg = levelBg(p.level)
              return (
                <div
                  key={i}
                  className="flex items-start gap-0 px-4 py-0.5 group"
                  style={{ background: bg }}
                >
                  {/* Line number */}
                  <span
                    className="shrink-0 w-6 text-right mr-4 select-none"
                    style={{ color: 'var(--text-tertiary)' }}
                  >
                    {i + 1}
                  </span>

                  {/* Timestamp */}
                  {p.timestamp && (
                    <span className="shrink-0 mr-3" style={{ color: 'var(--text-tertiary)' }}>
                      {p.timestamp}
                    </span>
                  )}

                  {/* Level badge */}
                  {p.level && (
                    <span
                      className="shrink-0 w-8 mr-3 font-semibold"
                      style={{ color: levelColor(p.level) }}
                    >
                      {p.level.slice(0, 4)}
                    </span>
                  )}

                  {/* Rest with syntax highlighting */}
                  <span className="break-all">{colorizeRest(p.rest)}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
