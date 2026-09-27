// Fails if a component reintroduces a hardcoded colour literal.
// Every file under app/ and components/ is guarded — the list is walked, not
// hand-maintained, so deleting or adding a component cannot rot this check.
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const ROOTS = ['app', 'components']

// SendReportDialog's modal scrim is deliberately a black rgba in both themes.
// globals.css is where the tokens themselves are defined.
const ALLOWED = new Set(['components/SendReportDialog.tsx'])

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walk(path))
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(path)
  }
  return out
}

const GUARDED = ROOTS.flatMap(walk).filter((f) => !ALLOWED.has(f)).sort()

const LITERAL = /#[0-9a-fA-F]{3,8}\b|rgba?\(/g
let failed = false

for (const file of GUARDED) {
  const hits = [...readFileSync(file, 'utf8').matchAll(LITERAL)]
  if (hits.length > 0) {
    failed = true
    console.error(`${file}: ${hits.length} colour literal(s): ${hits.map((h) => h[0]).join(', ')}`)
  }
}

if (failed) {
  console.error('\nUse a CSS token from globals.css instead of a literal.')
  process.exit(1)
}
console.log(`No colour literals in ${GUARDED.length} guarded file(s).`)
