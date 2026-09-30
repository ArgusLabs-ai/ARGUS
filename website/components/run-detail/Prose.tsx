/* Renders finding prose: `backticked` identifiers become <code>, and the
   one node named `who` is set as the culprit — the only red on the screen. */

import type { ReactNode } from 'react'

export default function Prose({
  text,
  who,
  whoTone = 'bad',
}: {
  text: string
  who?: string | null
  whoTone?: 'bad' | 'ok'
}) {
  const parts: ReactNode[] = []
  const re = /`([^`]+)`/g
  let last = 0
  let m: RegExpExecArray | null
  let whoShown = false
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const name = m[1]
    if (who && name === who && !whoShown) {
      whoShown = true
      parts.push(
        <span key={m.index} className="who" style={whoTone === 'ok' ? { color: 'var(--ok)' } : undefined}>
          {name}
        </span>,
      )
    } else {
      parts.push(<code key={m.index}>{name}</code>)
    }
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return <>{parts}</>
}
