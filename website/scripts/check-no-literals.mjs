// Fails if a restyled file reintroduces a hardcoded colour literal.
// Files graduate onto this list as Phase 2 de-hardcodes them.
import { readFileSync } from 'node:fs'

const GUARDED = [
  'lib/failure-labels.ts',
  'components/run-detail/StepInspector.tsx',
  'components/run-detail/StepInspectorSignals.tsx',
  'app/approvals/page.tsx',
  'app/approvals/ApprovalRows.tsx',
  'app/approvals/types.ts',
]

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
