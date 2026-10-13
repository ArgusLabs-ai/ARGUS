export const pct = (n: number, d: number) => (d ? `${Math.round((n / d) * 100)}%` : '—')

export const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export const short = (hash: string) => hash.slice(0, 8)

// Verdict → chip tone. Status words come from docs/STATUS.md.
export function statusClass(status: string): string {
  if (status === 'clean' || status === 'pass') return 'chip good'
  if (status === 'degraded' || status === 'retried' || status === 'interrupted') return 'chip warn'
  return 'chip bad'
}
