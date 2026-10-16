import type { Shape } from '@/lib/supabase'

// A card's value shape: type, length, features. Keys are already hashes.
export function shapeLabel(s: Shape): string {
  const parts = [s.t]
  if (s.t === 'bool' && s.v !== undefined) parts.push(String(s.v))
  if (s.len !== undefined) parts.push(`len ${s.len}`)
  if (s.f?.length) parts.push(s.f.join(' '))
  if (s.more) parts.push(`+${s.more} keys`)
  return parts.join(' · ')
}

export default function ShapeTree({ shape }: { shape: Shape }) {
  const keys = shape.keys ? Object.entries(shape.keys) : []
  return (
    <ul className="tree">
      <li>
        <span className={flagClass(shape)}>{shapeLabel(shape)}</span>
        {keys.length > 0 && (
          <ul className="tree">
            {keys.map(([k, v]) => (
              <li key={k}>
                <span className="muted">{k}</span> <ShapeInline shape={v} />
              </li>
            ))}
          </ul>
        )}
        {shape.item && (
          <ul className="tree">
            <li>
              <span className="muted">[item]</span> <ShapeInline shape={shape.item} />
            </li>
          </ul>
        )}
      </li>
    </ul>
  )
}

function ShapeInline({ shape }: { shape: Shape }) {
  if (shape.keys || shape.item) return <ShapeTree shape={shape} />
  return <span className={flagClass(shape)}>{shapeLabel(shape)}</span>
}

function flagClass(s: Shape): string {
  const f = s.f ?? []
  if (f.includes('error_word') || f.includes('refusal') || f.includes('placeholder')) return 'chip bad'
  if (f.includes('empty') || (s.t === 'array' && s.len === 0)) return 'chip warn'
  return ''
}
