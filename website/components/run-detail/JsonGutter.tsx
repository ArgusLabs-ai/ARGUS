/* JSON in the spec's editor idiom: a line-number gutter, syntax colour by
   token class, and an optional lint-style flag on the lines that matter. */

import type { ReactNode } from 'react'

export interface LintFlag {
  /** Top-level key (or dotted path) whose line gets the flag. */
  path: string
  note: string
  tone?: 'bad' | 'warn'
}

interface Line { nodes: ReactNode[]; path: string | null }

function tok(cls: string, text: string, key: number): ReactNode {
  return <span key={key} className={cls}>{text}</span>
}

function scalar(v: unknown, key: number): ReactNode {
  if (v === null) return tok('j-null', 'null', key)
  switch (typeof v) {
    case 'string': return tok('j-s', JSON.stringify(v), key)
    case 'number': return tok('j-n', String(v), key)
    case 'boolean': return tok('j-b', String(v), key)
    default: return tok('j-s', JSON.stringify(v) ?? String(v), key)
  }
}

function isPlain(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Serialises to lines the way JSON.stringify(v, null, 2) would, but keeps
    the dotted path of each line so flags can find it. */
export function jsonLines(value: unknown, maxDepth = 12): Line[] {
  const out: Line[] = []
  let k = 0
  const push = (indent: number, nodes: ReactNode[], path: string | null) => {
    out.push({ nodes: [tok('j-p', ' '.repeat(indent), k++), ...nodes], path })
  }
  const walk = (v: unknown, indent: number, path: string | null, prefix: ReactNode[], suffix: string) => {
    if (isPlain(v)) {
      const keys = Object.keys(v)
      if (!keys.length || indent / 2 >= maxDepth) {
        push(indent, [...prefix, tok('j-p', keys.length ? '{…}' : '{}', k++), tok('j-p', suffix, k++)], path)
        return
      }
      push(indent, [...prefix, tok('j-p', '{', k++)], path)
      keys.forEach((key, i) => {
        const child = path ? `${path}.${key}` : key
        const comma = i < keys.length - 1 ? ',' : ''
        walk(v[key], indent + 2, child, [tok('j-k', JSON.stringify(key), k++), tok('j-p', ': ', k++)], comma)
      })
      push(indent, [tok('j-p', '}' + suffix, k++)], null)
      return
    }
    if (Array.isArray(v)) {
      if (!v.length || indent / 2 >= maxDepth) {
        push(indent, [...prefix, tok('j-p', v.length ? '[…]' : '[]', k++), tok('j-p', suffix, k++)], path)
        return
      }
      push(indent, [...prefix, tok('j-p', '[', k++)], path)
      v.forEach((item, i) => {
        const comma = i < v.length - 1 ? ',' : ''
        walk(item, indent + 2, path ? `${path}[${i}]` : `[${i}]`, [], comma)
      })
      push(indent, [tok('j-p', ']' + suffix, k++)], null)
      return
    }
    push(indent, [...prefix, scalar(v, k++), tok('j-p', suffix, k++)], path)
  }
  walk(value, 0, null, [], '')
  return out
}

export default function JsonGutter({
  value,
  flags = [],
  maxLines,
  emptyText = 'empty',
}: {
  value: unknown
  flags?: LintFlag[]
  maxLines?: number
  emptyText?: string
}) {
  const empty = value == null || (isPlain(value) && !Object.keys(value).length) || (Array.isArray(value) && !value.length)
  if (empty) {
    return <div className="gut"><div className="gut-n">1</div><div className="gut-c"><span className="j-p">{emptyText}</span></div></div>
  }
  let lines = jsonLines(value)
  let truncated = 0
  if (maxLines && lines.length > maxLines) {
    truncated = lines.length - maxLines
    lines = lines.slice(0, maxLines)
  }
  const flagFor = (path: string | null) => (path ? flags.find((f) => f.path === path || path.endsWith(`.${f.path}`)) : undefined)

  return (
    <div className="gut">
      <div className="gut-n">
        {lines.map((_, i) => (i + 1)).join('\n')}
        {truncated > 0 && '\n…'}
      </div>
      <div className="gut-c">
        {lines.map((l, i) => {
          const f = flagFor(l.path)
          const body = <>{l.nodes}{f && <span className="j-note">   ← {f.note}</span>}</>
          return f ? (
            <span key={i} className={`j-hl${f.tone === 'warn' ? ' warn' : ''}`}>{body}{'\n'}</span>
          ) : (
            <span key={i}>{body}{'\n'}</span>
          )
        })}
        {truncated > 0 && <span className="j-p">… {truncated} more line{truncated === 1 ? '' : 's'}</span>}
      </div>
    </div>
  )
}
