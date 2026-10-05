'use client'

/* Logs as the spec's .log grid: line number, time, level word, message.
   Flush to the workspace edges; error and warning lines get a faint wash. */

import { useMemo } from 'react'

type Level = 'INFO' | 'WARN' | 'WARNING' | 'ERROR' | 'DEBUG' | 'FATAL'

interface Parsed { time: string; level: Level | null; rest: string }

function parseLine(line: string): Parsed {
  const m = line.match(/^(\S+)\s{2,}(INFO|WARN|WARNING|ERROR|DEBUG|FATAL)\s{2,}(.*)$/)
  if (m) return { time: m[1], level: m[2] as Level, rest: m[3] }
  const m2 = line.match(/^\[?(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]?\s+(INFO|WARN|WARNING|ERROR|DEBUG|FATAL)?\s*(.*)$/)
  if (m2) return { time: m2[1], level: (m2[2] as Level) ?? null, rest: m2[3] }
  return { time: '', level: null, rest: line }
}

function shortTime(t: string): string {
  const m = t.match(/(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)/)
  return m ? m[1] : t.slice(0, 12)
}

function levelClass(l: Level | null): string {
  switch (l) {
    case 'INFO': return 'lv-info'
    case 'DEBUG': return 'lv-info'
    case 'WARN': case 'WARNING': return 'lv-warn'
    case 'ERROR': case 'FATAL': return 'lv-err'
    default: return 'lv-info'
  }
}

function emphasise(rest: string): React.ReactNode[] {
  /* `backticked`, "quoted" and key=value tokens read one step brighter. */
  const parts: React.ReactNode[] = []
  const re = /(`[^`]+`)|("[^"]*")|(\b[\w.]+=(?:"[^"]*"|\S+))/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(rest)) !== null) {
    if (m.index > last) parts.push(rest.slice(last, m.index))
    parts.push(<b key={m.index}>{m[0].replace(/^`|`$/g, '')}</b>)
    last = m.index + m[0].length
  }
  if (last < rest.length) parts.push(rest.slice(last))
  return parts
}

export default function CliLogViewer({ log }: { log: string; runId?: string }) {
  const lines = useMemo(() => log.split('\n').filter((l) => l.trim()).map(parseLine), [log])
  const errors = lines.filter((l) => l.level === 'ERROR' || l.level === 'FATAL').length
  const warns = lines.filter((l) => l.level === 'WARN' || l.level === 'WARNING').length

  return (
    <div>
      <p className="cap">
        <span>
          {lines.length} lines
          {errors > 0 && <> · <span style={{ color: 'var(--tool)' }}>{errors} error{errors === 1 ? '' : 's'}</span></>}
          {warns > 0 && <> · {warns} warning{warns === 1 ? '' : 's'}</>}
        </span>
        <a href="#" onClick={(e) => { e.preventDefault(); navigator.clipboard?.writeText(log).catch(() => {}) }}>Copy</a>
      </p>
      <div className="log">
        {lines.map((l, i) => {
          const wash = l.level === 'ERROR' || l.level === 'FATAL' ? ' err' : l.level === 'WARN' || l.level === 'WARNING' ? ' warn' : ''
          return (
            <div key={i} className={`log-line${wash}`}>
              <span className="log-n">{i + 1}</span>
              <span className="log-t">{shortTime(l.time)}</span>
              <span className={`log-lv ${levelClass(l.level)}`}>{l.level ? l.level.slice(0, 4) : ''}</span>
              <span className="log-m">{emphasise(l.rest)}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
