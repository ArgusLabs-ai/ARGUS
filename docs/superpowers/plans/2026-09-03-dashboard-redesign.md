# Dashboard Refactor + Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle the ARGUS dashboard to the "Argus Instrument" interface spec (Apple/macOS language, dual light+dark theme) and close the six data gaps where the UI has drifted behind the backend.

**Architecture:** Reskin, not rebuild. Every component the spec needs already exists in `website/`. Work proceeds token-first: port the spec's CSS variables, alias the old token names onto the new ones so existing Tailwind classes keep working, then restyle components phase by phase. The one true refactor is de-hardcoding ~340 hex literals that currently make theming impossible. A Python parity test prevents the backend↔UI drift that caused the data gaps in the first place.

**Tech Stack:** Next.js 14 (App Router), React 18, Tailwind (CSS-variable tokens), `@xyflow/react` for the graph, `radix-ui` + `lucide-react`. No new dependencies. Verification via `tsc --noEmit`, `next build`, and pytest.

**Spec:** https://claude.ai/code/artifact/890ebef5-354e-4049-b4ad-cc6d989d1fbc
**Branch:** `dashboard-redesign` (already created off `master`)

**Getting the spec CSS:** several tasks say "copy the `.foo` classes verbatim from the
artifact". The artifact is ~220KB and is the single source of truth for exact values,
so it is referenced rather than inlined here. Fetch it with `WebFetch` on the URL above
(it is fetchable with the owner's claude.ai login; `curl` will not work — it returns
the SPA shell). Save a local copy once at the start of Phase 1 and work from that, so
every task reads identical values. Each task lists the exact class names to copy, so
nothing is left to judgement.

---

## Context an engineer needs before starting

**What ARGUS is:** a production-readiness platform for AI agent pipelines. It wraps
each node of a LangGraph pipeline, watches what the node returns, and records a
`RunRecord` to `.argus/runs/<run-id>.json`. The dashboard reads those JSON files and
shows *why a run failed*. Read `CLAUDE.md` and `docs/STATUS.md` first.

**Where the dashboard gets data:** `website/lib/data.ts` loads run records; `website/data/index.json`
holds fixtures. `website/lib/types.ts` is a hand-maintained TypeScript mirror of the
Python dataclasses in `src/argus/models.py`. **These two drift.** That drift is the
root cause of gaps G1–G3 below, and Task 1 makes it impossible to drift silently again.

**Current styling state (this is why the refactor is needed):**
- `app/globals.css` is **dark-only**: `:root { color-scheme: dark }`, no
  `prefers-color-scheme`, no `[data-theme]`, no toggle. The spec requires both themes.
- **~340 hardcoded hex literals** live inside `.tsx` files (worst: `StepInspector.tsx`
  with 69, `approvals/page.tsx` with 37). A hardcoded `#ef4444` cannot respond to a
  theme change. The spec's own rule: *"Nothing declares a colour literal — every rule
  reads a token."* De-hardcoding is the bulk of the refactor.
- `html, body { overflow: hidden }` — this is an app shell, not a scrolling page.

**Testing reality:** the website has **no test framework** and none is being added
(YAGNI — we are not unit-testing CSS). Verification is three things:
1. `npx --no-install tsc --noEmit` — must stay at 0 errors (it is clean today).
2. `npm run build` — must succeed.
3. `pytest tests/test_ui_parity.py` — the new guard from Task 1.
Visual checks are done by eye in both themes, called out per task.

**Commit style:** conventional commits, one per task. Never add "co-authored by Claude"
(see `CLAUDE.md`).

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `tests/test_ui_parity.py` | Pytest guard: every failure type / behavior profile / status the Python side emits has a UI label. Prevents backend↔UI drift. |
| `website/lib/theme.ts` | Theme resolution + persistence (`light`/`dark`/`system` → `[data-theme]`). |
| `website/components/ThemeToggle.tsx` | The segmented control that writes `[data-theme]`. |
| `website/components/FindingsPanel.tsx` | Renders `RunRecord.findings` — the canonical failure list (gap G1). |
| `website/lib/check.ts` | Client-side port of `argus check` grading, for the CI-verdict chip (gap G4). |
| `website/scripts/check-no-literals.mjs` | Guard script: fails if hex literals remain in restyled components. |

**Modified (major):**

| File | Change |
|---|---|
| `website/app/globals.css` | Replace the dark-only token block with the spec's full dual-theme token set. The single highest-leverage file. |
| `website/tailwind.config.ts` | Alias old token names → new spec tokens so existing classes keep working. |
| `website/lib/types.ts` | Add missing union members + `RunRecord` fields (G2, G3, G6). |
| `website/lib/failure-labels.ts` | Add `empty_output`, `json_in_string`; retarget colors to tokens (G2). |
| `website/components/run-detail/StepInspector.tsx` | 795 lines, 69 literals — de-hardcode and split. |
| `website/app/approvals/page.tsx` | 1027 lines, 37 literals — de-hardcode and split. |

**Split (files that have grown unwieldy and are being touched anyway):**
- `StepInspector.tsx` (795) → keep the shell, extract `StepInspectorSignals.tsx` and `StepInspectorPayload.tsx`.
- `approvals/page.tsx` (1027) → extract `approvals/ApprovalCard.tsx` and `approvals/ApprovalFilters.tsx`.

Files not listed are touched only by mechanical token swaps.

---

## Phase 0 — Stop the drift, fix the data gaps

### Task 1: Parity guard between Python enums and UI labels

The audit found `empty_output` and `json_in_string` rendering as a grey "Unknown"
chip, and `chat_response`/`code_generation` rendering as raw snake_case — because
someone added a detection rule in Python and nobody updated the TypeScript. Rather
than patch the four values and wait for the next drift, we test the invariant.

**Files:**
- Create: `tests/test_ui_parity.py`
- Read only: `src/argus/inspector.py`, `src/argus/anomaly_detector.py`, `website/lib/failure-labels.ts`, `website/components/run-detail/BehaviorPanel.tsx`

- [ ] **Step 1: Write the failing test**

```python
"""Guard: every enum value the Python side emits must have a UI label.

The dashboard mirrors Python enums by hand in TypeScript. When a detection rule
is added in `inspector.py` without a matching entry in `failure-labels.ts`, the UI
silently renders a grey "Unknown" chip. These tests fail instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEBSITE = REPO / "website"

pytestmark = pytest.mark.unit


def _python_failure_types() -> set[str]:
    """Every failure_type literal emitted anywhere in src/argus."""
    found: set[str] = set()
    for path in (REPO / "src" / "argus").rglob("*.py"):
        found |= set(re.findall(r'failure_type="([a-z_]+)"', path.read_text()))
    # Category-mapped types never appear as a literal at the ToolFailure call site.
    inspector = (REPO / "src" / "argus" / "inspector.py").read_text()
    block = re.search(r"_CATEGORY_TO_FAILURE.*?\n}", inspector, re.S)
    assert block, "_CATEGORY_TO_FAILURE not found in inspector.py"
    found |= set(re.findall(r':\s*"([a-z_]+)"', block.group(0)))
    return found


def _ui_failure_labels() -> set[str]:
    """Keys of FAILURE_META in website/lib/failure-labels.ts."""
    src = (WEBSITE / "lib" / "failure-labels.ts").read_text()
    block = re.search(r"FAILURE_META:\s*Record<string,\s*FailureMeta>\s*=\s*{(.*?)\n}", src, re.S)
    assert block, "FAILURE_META object not found in failure-labels.ts"
    return set(re.findall(r"^\s*([a-z_]+):\s*{", block.group(1), re.M))


def _python_behavior_types() -> set[str]:
    src = (REPO / "src" / "argus" / "anomaly_detector.py").read_text()
    known = {
        "structured_json", "retrieval_result", "classification", "detailed_text",
        "tool_output", "reasoning_chain", "chat_response", "code_generation",
    }
    return {name for name in known if f'"{name}"' in src}


def _ui_behavior_labels() -> set[str]:
    src = (WEBSITE / "components" / "run-detail" / "BehaviorPanel.tsx").read_text()
    block = re.search(r"BEHAVIOR_LABELS:\s*Record<string,\s*string>\s*=\s*{(.*?)\n}", src, re.S)
    assert block, "BEHAVIOR_LABELS object not found in BehaviorPanel.tsx"
    return set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))


def test_every_failure_type_has_a_ui_label() -> None:
    missing = _python_failure_types() - _ui_failure_labels()
    assert not missing, (
        f"failure types with no UI label (they render as grey 'Unknown'): "
        f"{sorted(missing)}. Add them to website/lib/failure-labels.ts."
    )


def test_every_behavior_type_has_a_ui_label() -> None:
    missing = _python_behavior_types() - _ui_behavior_labels()
    assert not missing, (
        f"behavior profiles with no UI label (they render as raw snake_case): "
        f"{sorted(missing)}. Add them to BEHAVIOR_LABELS in BehaviorPanel.tsx."
    )


def test_every_step_status_is_rendered() -> None:
    """All StepStatus values must appear in StatusBadge.tsx."""
    models = (REPO / "src" / "argus" / "models.py").read_text()
    block = re.search(r"StepStatus\s*=\s*Literal\[(.*?)\]", models, re.S)
    assert block, "StepStatus Literal not found in models.py"
    statuses = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    badge = (WEBSITE / "components" / "StatusBadge.tsx").read_text()
    missing = {s for s in statuses if s not in badge}
    assert not missing, f"step statuses not handled in StatusBadge.tsx: {sorted(missing)}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_ui_parity.py -v`

Expected: `test_every_failure_type_has_a_ui_label` FAILS listing
`['empty_output', 'json_in_string']`, and `test_every_behavior_type_has_a_ui_label`
FAILS listing `['chat_response', 'code_generation']`.
`test_every_step_status_is_rendered` should PASS — StatusBadge already covers all 8.

If a test errors on a missing regex match instead of failing on the assertion, the
parsing is wrong, not the code under test — fix the regex before continuing.

- [ ] **Step 3: Add the two missing failure labels**

In `website/lib/failure-labels.ts`, add `empty_output` to the Tool group (it is a
critical hard failure — a node returned a literal `{}` while successors waited) and
`json_in_string` to the Quality group (Rule 17 is advisory/warning-only):

```typescript
  // Tool — hard errors from external calls
  error_response:     { label: 'Error Response',     category: 'Tool',      categoryColor: '#ef4444' },
  rate_limit:         { label: 'Rate Limited',        category: 'Tool',      categoryColor: '#ef4444' },
  empty_result:       { label: 'Empty Result',        category: 'Tool',      categoryColor: '#ef4444' },
  empty_output:       { label: 'Empty Output',        category: 'Tool',      categoryColor: '#ef4444' },
  error_in_data:      { label: 'Error in Data',       category: 'Tool',      categoryColor: '#ef4444' },
  partial_failure:    { label: 'Partial Failure',     category: 'Tool',      categoryColor: '#ef4444' },
```

and in the Quality group:

```typescript
  json_in_string:                  { label: 'Double-Encoded JSON', category: 'Quality', categoryColor: '#f59e0b' },
```

- [ ] **Step 4: Add the two missing behavior labels**

In `website/components/run-detail/BehaviorPanel.tsx`, replace the `BEHAVIOR_LABELS`
object (currently at line 8) with:

```typescript
const BEHAVIOR_LABELS: Record<string, string> = {
  structured_json: 'Structured JSON',
  retrieval_result: 'Retrieval Result',
  classification: 'Classification',
  detailed_text: 'Detailed Text',
  tool_output: 'Tool Output',
  reasoning_chain: 'Reasoning Chain',
  chat_response: 'Chat Response',
  code_generation: 'Code Generation',
}
```

- [ ] **Step 5: Widen the TypeScript unions to match**

In `website/lib/types.ts`, add `empty_output` and `json_in_string` to the
`ToolFailure.failure_type` union (place `empty_output` after `empty_result`, and
`json_in_string` after `truncated_output`), and extend `BehaviorType`:

```typescript
export type BehaviorType =
  | 'structured_json'
  | 'retrieval_result'
  | 'classification'
  | 'detailed_text'
  | 'tool_output'
  | 'reasoning_chain'
  | 'chat_response'
  | 'code_generation'
```

- [ ] **Step 6: Run the tests and the typechecker to verify they pass**

```bash
pytest tests/test_ui_parity.py -v
cd website && npx --no-install tsc --noEmit
```

Expected: all three parity tests PASS; `tsc` prints nothing and exits 0.

- [ ] **Step 7: Commit**

```bash
git add tests/test_ui_parity.py website/lib/failure-labels.ts website/lib/types.ts website/components/run-detail/BehaviorPanel.tsx
git commit -m "fix(ui): label empty_output, json_in_string and the two new behavior profiles

Adds a pytest parity guard so a detection rule added in Python can no longer
land without a matching UI label."
```

---

### Task 2: Fill the remaining typed-but-absent RunRecord fields

Gap G6. `state_patch` (time-travel patching, PR #29), `coverage_summary`,
`schema_version` and `app_factory_ref` exist on the Python `RunRecord` but not the
TypeScript one, so the UI cannot read them even where it would be useful.

**Files:**
- Modify: `website/lib/types.ts` (the `RunRecord` interface)

- [ ] **Step 1: Add the fields**

Add to the `RunRecord` interface, after `dry_run?: boolean`:

```typescript
  schema_version?: string
  state_patch?: Record<string, unknown> | null
  coverage_summary?: Record<string, number>
  app_factory_ref?: string | null
```

All optional — older records on disk will not have them, and `storage.py` back-fills
on load rather than rewriting files.

- [ ] **Step 2: Verify the typechecker still passes**

Run: `cd website && npx --no-install tsc --noEmit`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add website/lib/types.ts
git commit -m "types(ui): mirror state_patch, coverage_summary and schema_version from RunRecord"
```

---

## Phase 1 — Token foundation (unblocks everything else)

### Task 3: Port the spec's dual-theme token set

This is the highest-leverage change in the plan. The spec defines the complete palette
three times — bare `:root` (light), `@media (prefers-color-scheme: dark)` guarded with
`:not([data-theme="light"])`, and `[data-theme="dark"]` — so that an explicit user
choice wins in both directions. Copy that structure exactly; it is load-bearing.

**Files:**
- Modify: `website/app/globals.css:13-88` (replace the whole `:root` block)

- [ ] **Step 1: Replace the `:root` block with the spec's light palette**

Delete the existing `:root { color-scheme: dark; ... }` block (lines 13–88, ending at
the closing brace after the legacy compatibility aliases) and paste the spec's token
set. Copy the values verbatim from the artifact's `:root` block — grounds
(`--rail`, `--ex`, `--band`, `--void`, `--panel`, `--raised`, `--raised-hover`,
`--line`, `--line-2`, `--line-3`), ink hierarchy (`--ink` … `--ink-4`), accent
(`--iris`, `--iris-bright`, `--iris-dim`, `--iris-line`, `--on-accent`), signals
(`--ok`, `--tool`, `--quality`, `--semantic`, `--coherence`, `--idle`), the `-dim`
and `surf-` variants, `--glow-*`, `--edge-*`, code/diff tokens (`--j-key`, `--j-str`,
`--j-num`, `--j-bool`, `--diff-*`, `--sk-hi`), shadows, `--sans`/`--mono`, radii
(`--r-chip`, `--r-ctl`, `--r-panel`), and motion (`--ease`, `--fast`, `--med`).

Note `color-scheme: light` on the bare `:root` — the dashboard is currently
dark-only, so this flips the default. That is intended.

- [ ] **Step 2: Append the two dark blocks**

Directly after the `:root` block, add the spec's
`@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { … } }` block
and then the `:root[data-theme="dark"] { … }` block, both verbatim from the artifact.
The `:not([data-theme="light"])` guard is what lets a user's explicit light choice
beat the OS preference — do not simplify it away.

- [ ] **Step 3: Add the legacy alias bridge**

The codebase has hundreds of `bg-card`, `text-muted-foreground`, `border-border`
usages. Rather than rewrite them all now, alias the old names onto the new tokens.
Append this block after the dark blocks so it inherits whichever palette is active:

```css
/* ── Bridge: old token names → spec tokens ─────────────────────────
   Lets existing Tailwind classes keep working while components are
   restyled phase by phase. Delete a line once no component reads it.  */
:root, :root[data-theme="dark"] {
  --background:             var(--void);
  --foreground:             var(--ink);
  --card:                   var(--panel);
  --card-foreground:        var(--ink);
  --popover:                var(--raised);
  --popover-foreground:     var(--ink);
  --primary:                var(--iris);
  --primary-foreground:     var(--on-accent);
  --secondary:              var(--panel);
  --secondary-foreground:   var(--ink);
  --muted:                  var(--panel);
  --muted-foreground:       var(--ink-3);
  --accent:                 var(--panel);
  --accent-foreground:      var(--ink);
  --destructive:            var(--tool);
  --destructive-foreground: var(--on-signal);
  --success:                var(--ok);
  --success-foreground:     var(--on-signal);
  --warning:                var(--quality);
  --warning-foreground:     var(--on-signal);
  --border:                 var(--line);
  --input:                  var(--line-2);
  --ring:                   var(--iris);
  --radius:                 var(--r-panel);

  --code-bg:                var(--void);
  --code-header:            var(--band);
  --text-secondary:         var(--ink-2);
  --text-tertiary:          var(--ink-3);
  --failure:                var(--tool);
  --running:                var(--iris);

  --sidebar:                var(--rail);
  --sidebar-foreground:     var(--ink-2);
  --sidebar-primary:        var(--iris);
  --sidebar-primary-foreground: var(--on-accent);
  --sidebar-accent:         var(--hover);
  --sidebar-accent-foreground: var(--ink);
  --sidebar-border:         var(--line);
  --sidebar-ring:           var(--iris);

  --bg-base:        var(--void);
  --bg-surface:     var(--panel);
  --bg-elevated:    var(--raised);
  --bg-overlay:     var(--overlay);
  --border-subtle:  var(--line);
  --border-default: var(--line);
  --border-strong:  var(--line-3);
  --text-primary:   var(--ink);
  --text-muted:     var(--ink-3);
  --text-faint:     var(--ink-4);
  --accent-blue:    var(--iris);
  --accent-blue-dim: var(--iris-dim);
  --accent-green:   var(--ok);
  --accent-red:     var(--tool);
  --accent-amber:   var(--quality);
  --accent-magenta: var(--semantic);
  --accent-cyan:    var(--coherence);
  --sidebar-bg:     var(--rail);
  --sidebar-active: var(--iris-dim);
  --sidebar-text:   var(--ink-2);
  --sidebar-muted:  var(--ink-3);
}
```

- [ ] **Step 4: Retarget the JSON syntax colors to tokens**

The JSON highlighting rules further down `globals.css` are hardcoded. Replace them:

```css
/* ── JSON viewer syntax highlighting ───────────────────────────── */
.json-key     { color: var(--j-key); }
.json-string  { color: var(--j-str); }
.json-number  { color: var(--j-num); }
.json-boolean { color: var(--j-bool); }
.json-null    { color: var(--ink-4); }
```

and the thin scrollbar thumb, which is also a literal:

```css
.scrollbar-thin::-webkit-scrollbar-thumb { background-color: var(--line-2); border-radius: 4px; }
```

- [ ] **Step 5: Point the font stacks at the spec**

The spec uses SF Pro for the interface and SF Mono for machine values only. Both are
system fonts on macOS with sensible fallbacks — no bundle cost. In
`website/tailwind.config.ts`, replace the `fontFamily` block:

```typescript
      fontFamily: {
        sans: ['var(--sans)'],
        mono: ['var(--mono)'],
      },
```

`--sans` and `--mono` are already defined by the token block from Step 1. Leave the
`next/font` setup in `app/layout.tsx` alone for now; Task 4 removes it if unused.

- [ ] **Step 6: Verify the build and eyeball both themes**

```bash
cd website
npx --no-install tsc --noEmit
npm run build
npm run dev
```

Expected: typecheck and build both succeed. Open `http://localhost:3000`, then toggle
your OS between light and dark appearance. The app should re-tint in both directions
and remain legible. It will look *wrong* — old component styling on new tokens — but
nothing should be invisible, and no element should keep a dark background while the
page goes light. Any element that does is reading a hardcoded literal; note it for
Phase 2 rather than fixing it here.

- [ ] **Step 7: Commit**

```bash
git add website/app/globals.css website/tailwind.config.ts
git commit -m "feat(ui): port the Argus Instrument dual-theme token set

Adds the full light/dark palette with a bridge aliasing the old token names,
so existing components keep rendering while they are restyled."
```

---

### Task 4: Theme resolution and toggle

The spec is explicit that a user's toggle must beat the OS preference in *both*
directions, which is why Task 3 added the `[data-theme]` selectors. This task wires
them up.

**Files:**
- Create: `website/lib/theme.ts`
- Create: `website/components/ThemeToggle.tsx`
- Modify: `website/app/layout.tsx`
- Modify: `website/components/Topbar.tsx`

- [ ] **Step 1: Write the theme module**

```typescript
// website/lib/theme.ts
export type Theme = 'light' | 'dark' | 'system'

const KEY = 'argus-theme'

export function getStoredTheme(): Theme {
  if (typeof window === 'undefined') return 'system'
  const raw = window.localStorage.getItem(KEY)
  return raw === 'light' || raw === 'dark' ? raw : 'system'
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement
  if (theme === 'system') {
    root.removeAttribute('data-theme')
    window.localStorage.removeItem(KEY)
  } else {
    root.setAttribute('data-theme', theme)
    window.localStorage.setItem(KEY, theme)
  }
}
```

- [ ] **Step 2: Prevent the flash of wrong theme**

A stored theme is only readable client-side, so the first paint would use the OS
theme and then snap. Add this blocking script to `<head>` in
`website/app/layout.tsx` — it must run before first paint, so it is inline and
synchronous by design:

```tsx
<script
  dangerouslySetInnerHTML={{
    __html: `(function(){try{var t=localStorage.getItem('argus-theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t)}catch(e){}})()`,
  }}
/>
```

- [ ] **Step 3: Write the toggle using the spec's segmented control**

```tsx
// website/components/ThemeToggle.tsx
'use client'

import { useEffect, useState } from 'react'
import { applyTheme, getStoredTheme, type Theme } from '@/lib/theme'

const OPTIONS: Theme[] = ['light', 'dark', 'system']

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>('system')

  useEffect(() => setTheme(getStoredTheme()), [])

  function choose(next: Theme) {
    setTheme(next)
    applyTheme(next)
  }

  return (
    <div className="seg" role="group" aria-label="Colour theme">
      {OPTIONS.map((option) => (
        <button
          key={option}
          type="button"
          aria-selected={theme === option}
          onClick={() => choose(option)}
        >
          {option[0].toUpperCase() + option.slice(1)}
        </button>
      ))}
    </div>
  )
}
```

The `.seg` class comes from the spec's primitives — Task 6 defines it. Until then the
control renders unstyled but functional.

- [ ] **Step 4: Mount it in the top bar**

Import `ThemeToggle` in `website/components/Topbar.tsx` and render it in the
right-hand control cluster, alongside the existing actions.

- [ ] **Step 5: Verify**

```bash
cd website && npx --no-install tsc --noEmit && npm run dev
```

Check all four cases by hand — they are the ones the `:not([data-theme="light"])`
guard exists for:
1. OS dark + toggle "Light" → page is light.
2. OS light + toggle "Dark" → page is dark.
3. Toggle "System" → follows the OS, and changing OS appearance updates it live.
4. Pick "Light", hard-reload → still light with no dark flash on first paint.

- [ ] **Step 6: Commit**

```bash
git add website/lib/theme.ts website/components/ThemeToggle.tsx website/app/layout.tsx website/components/Topbar.tsx
git commit -m "feat(ui): add light/dark/system theme toggle with no-flash hydration"
```

---

## Phase 2 — The refactor: de-hardcode colour literals

### Task 5: Literal guard script

~340 hex literals defeat theming. Fix them file by file, but add the ratchet first so
they cannot come back.

**Files:**
- Create: `website/scripts/check-no-literals.mjs`
- Modify: `website/package.json`

- [ ] **Step 1: Write the guard**

```javascript
// website/scripts/check-no-literals.mjs
// Fails if a restyled file reintroduces a hardcoded colour literal.
// Files graduate onto this list as Phase 2 de-hardcodes them.
import { readFileSync } from 'node:fs'

const GUARDED = [
  'lib/failure-labels.ts',
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
```

- [ ] **Step 2: Add the npm script**

In `website/package.json`, add to `"scripts"`:

```json
    "check:literals": "node scripts/check-no-literals.mjs",
```

- [ ] **Step 3: Run it and watch it fail**

Run: `cd website && npm run check:literals`
Expected: FAILS — `lib/failure-labels.ts` still carries `#ef4444`, `#f59e0b`,
`#a855f7`, `#6366f1`, `#6b7280` in its `categoryColor` fields.

- [ ] **Step 4: Move failure-labels onto tokens**

`categoryColor` is consumed as an inline style value, so it must hold something CSS
resolves. Swap each literal for the matching signal token — the spec's category
colours line up with the existing four categories exactly:

```typescript
export const FAILURE_META: Record<string, FailureMeta> = {
  // Tool — hard errors from external calls
  error_response:     { label: 'Error Response',     category: 'Tool',      categoryColor: 'var(--tool)' },
  rate_limit:         { label: 'Rate Limited',        category: 'Tool',      categoryColor: 'var(--tool)' },
  empty_result:       { label: 'Empty Result',        category: 'Tool',      categoryColor: 'var(--tool)' },
  empty_output:       { label: 'Empty Output',        category: 'Tool',      categoryColor: 'var(--tool)' },
  error_in_data:      { label: 'Error in Data',       category: 'Tool',      categoryColor: 'var(--tool)' },
  partial_failure:    { label: 'Partial Failure',     category: 'Tool',      categoryColor: 'var(--tool)' },
  // Quality — output exists but is degraded
  truncated_output:                { label: 'Truncated',            category: 'Quality', categoryColor: 'var(--quality)' },
  json_in_string:                  { label: 'Double-Encoded JSON',  category: 'Quality', categoryColor: 'var(--quality)' },
  confidence_mismatch:             { label: 'Confidence Mismatch',  category: 'Quality', categoryColor: 'var(--quality)' },
  retrieval_quality_low:           { label: 'Low Retrieval',        category: 'Quality', categoryColor: 'var(--quality)' },
  shallow_context:                 { label: 'Shallow Context',      category: 'Quality', categoryColor: 'var(--quality)' },
  shallow_output:                  { label: 'Shallow Output',       category: 'Quality', categoryColor: 'var(--quality)' },
  information_compression_anomaly: { label: 'Over-Compressed',      category: 'Quality', categoryColor: 'var(--quality)' },
  timeout_adjacent:                { label: 'Near Timeout',         category: 'Quality', categoryColor: 'var(--quality)' },
  suspiciously_fast:               { label: 'Suspiciously Fast',    category: 'Quality', categoryColor: 'var(--quality)' },
  latency_quality_mismatch:        { label: 'Fast + Failed',        category: 'Quality', categoryColor: 'var(--quality)' },
  // Semantic — LLM output smells
  placeholder_detected: { label: 'Placeholder', category: 'Semantic', categoryColor: 'var(--semantic)' },
  semantic_degradation: { label: 'Degradation', category: 'Semantic', categoryColor: 'var(--semantic)' },
  structural_anomaly:   { label: 'Structural',  category: 'Semantic', categoryColor: 'var(--semantic)' },
  // Coherence — input-output relationship issues
  selective_attention_reduction: { label: 'Selective Attention', category: 'Coherence', categoryColor: 'var(--coherence)' },
  input_echo:                    { label: 'Input Echo',          category: 'Coherence', categoryColor: 'var(--coherence)' },
  semantic_contradiction:        { label: 'Contradiction',       category: 'Coherence', categoryColor: 'var(--coherence)' },
  context_size_anomaly:          { label: 'Context Overflow',    category: 'Coherence', categoryColor: 'var(--coherence)' },
}

const FALLBACK: FailureMeta = { label: 'Unknown', category: 'Tool', categoryColor: 'var(--idle)' }
```

- [ ] **Step 5: Run the guard and the parity test**

```bash
cd website && npm run check:literals && npx --no-install tsc --noEmit
cd .. && pytest tests/test_ui_parity.py -v
```

Expected: guard prints "No colour literals in 1 guarded file(s)."; typecheck clean;
parity tests still pass.

- [ ] **Step 6: Commit**

```bash
git add website/scripts/check-no-literals.mjs website/package.json website/lib/failure-labels.ts
git commit -m "refactor(ui): move failure labels onto colour tokens, add a literal guard"
```

---

### Task 6: De-hardcode and split StepInspector

Worst offender: 795 lines, 69 literals. It is being restyled anyway, so split it.

**Files:**
- Modify: `website/components/run-detail/StepInspector.tsx`
- Create: `website/components/run-detail/StepInspectorSignals.tsx`
- Create: `website/components/run-detail/StepInspectorPayload.tsx`
- Modify: `website/scripts/check-no-literals.mjs`

- [ ] **Step 1: Read the file and map its sections**

Run: `cd website && npx --no-install tsc --noEmit` first to confirm a clean baseline,
then read `components/run-detail/StepInspector.tsx` end to end. Identify three
regions: the header/status shell, the signal lists (inspection findings, validator
results, anomaly signals, semantic check), and the payload viewers (input state,
output dict, diff). Do not start cutting until you can name where each region begins
and ends — this file has interleaved conditional rendering.

- [ ] **Step 2: Extract the signals region**

Move the signal-rendering JSX into `StepInspectorSignals.tsx` as a component taking
`{ step }: { step: NodeEvent }`. Import `NodeEvent` from `@/lib/types`. Keep the
props surface to exactly what the JSX reads — do not pass the whole run.

- [ ] **Step 3: Extract the payload region**

Move the input/output/diff viewers into `StepInspectorPayload.tsx` with the same
prop discipline. It re-uses the existing `JsonViewer` component — import it, do not
reimplement JSON rendering.

- [ ] **Step 4: Replace every literal with a token**

Across all three files, map each literal to its token. The recurring ones in this
file are:

| Literal | Token |
|---|---|
| `#22c55e` | `var(--ok)` |
| `#ef4444` | `var(--tool)` |
| `#f59e0b` | `var(--quality)` |
| `#a855f7` | `var(--semantic)` |
| `#6366f1` / `#5b6af0` / `#818cf8` | `var(--iris)` |
| `#6b7280` / `#888888` | `var(--ink-3)` |

For `rgba(...)` tints used as backgrounds, use the matching `-dim` token
(`var(--ok-dim)`, `var(--tool-dim)`, `var(--quality-dim)`, `var(--semantic-dim)`,
`var(--iris-dim)`) rather than inventing a new alpha. Where a tint has no `-dim`
equivalent, use `color-mix(in srgb, var(--token) 12%, transparent)` — the spec uses
this pattern for chip borders.

The biggest single cluster is the map inside the `statusBadge()` helper at
`StepInspector.tsx:25`. Keep the function name and signature exactly as they are —
it has call sites elsewhere in the file — and replace only the map body:

```tsx
function statusBadge(status: string) {
  const map: Record<string, { label: string; color: string; bg: string }> = {
    pass:           { label: 'Passed',        color: 'var(--ok)',        bg: 'var(--ok-dim)' },
    crashed:        { label: 'Crashed',       color: 'var(--tool)',      bg: 'var(--tool-dim)' },
    fail:           { label: 'Failed',        color: 'var(--tool)',      bg: 'var(--tool-dim)' },
    semantic_fail:  { label: 'Semantic Fail', color: 'var(--semantic)',  bg: 'var(--semantic-dim)' },
    interrupted:    { label: 'Interrupted',   color: 'var(--quality)',   bg: 'var(--quality-dim)' },
    degraded_input: { label: 'Degraded',      color: 'var(--quality)',   bg: 'var(--quality-dim)' },
    skipped:        { label: 'Skipped',       color: 'var(--ink-3)',     bg: 'var(--fill-subtle)' },
    retried:        { label: 'Retried',       color: 'var(--ink-3)',     bg: 'var(--fill-subtle)' },
  }
  return map[status] ?? { label: status, color: 'var(--ink-3)', bg: 'var(--fill-subtle)' }
}
```

Labels and status→colour pairings are preserved from the original — this step changes
colour *sourcing* only. Any recolouring is Task 9's job, where `StatusBadge` becomes
the single source of status colour and this local map can be deleted.

- [ ] **Step 5: Add the three files to the guard**

In `website/scripts/check-no-literals.mjs`, extend `GUARDED`:

```javascript
const GUARDED = [
  'lib/failure-labels.ts',
  'components/run-detail/StepInspector.tsx',
  'components/run-detail/StepInspectorSignals.tsx',
  'components/run-detail/StepInspectorPayload.tsx',
]
```

- [ ] **Step 6: Verify**

```bash
cd website
npm run check:literals
npx --no-install tsc --noEmit
npm run build
```

Expected: all three succeed. Then `npm run dev`, open a run with a failing step, and
confirm the inspector renders identically to before in dark mode and is legible in
light mode.

- [ ] **Step 7: Commit**

```bash
git add website/components/run-detail/StepInspector.tsx website/components/run-detail/StepInspectorSignals.tsx website/components/run-detail/StepInspectorPayload.tsx website/scripts/check-no-literals.mjs
git commit -m "refactor(ui): split StepInspector and move it onto colour tokens"
```

---

### Task 7: De-hardcode and split the approvals page

1027 lines, 37 literals — the largest file in the dashboard.

**Files:**
- Modify: `website/app/approvals/page.tsx`
- Create: `website/app/approvals/ApprovalCard.tsx`
- Create: `website/app/approvals/ApprovalFilters.tsx`
- Modify: `website/scripts/check-no-literals.mjs`

- [ ] **Step 1: Extract the per-approval card**

Move the JSX that renders one approval item into `ApprovalCard.tsx`. Keep the page
responsible for data fetching and state; the card takes its item plus any action
callbacks as props and holds no fetching logic of its own.

- [ ] **Step 2: Extract the filter/toolbar region**

Move the filter controls into `ApprovalFilters.tsx`, taking current filter state and
an `onChange` callback. Lift no state into it — the page stays the owner.

- [ ] **Step 3: Replace every literal with a token**

Use the same mapping table as Task 6, Step 4.

- [ ] **Step 4: Add all three files to the guard**

Append `'app/approvals/page.tsx'`, `'app/approvals/ApprovalCard.tsx'` and
`'app/approvals/ApprovalFilters.tsx'` to `GUARDED`.

- [ ] **Step 5: Verify**

```bash
cd website && npm run check:literals && npx --no-install tsc --noEmit && npm run build
```

Expected: all succeed. Visually confirm `/approvals` in both themes.

- [ ] **Step 6: Commit**

```bash
git add website/app/approvals website/scripts/check-no-literals.mjs
git commit -m "refactor(ui): split the approvals page and move it onto colour tokens"
```

---

### Task 8: De-hardcode the remaining components

The long tail: `StepCard.tsx` (30), `ExecutionGraph.tsx` (22), `PipelineOverview.tsx`
(20), `StatusBadge.tsx` (20), `MetricsGrid.tsx` (17), `ReplayBranches.tsx` (15),
`SendReportDialog.tsx` (12), `AIAnalysisTab.tsx` (12), `CycleGroup.tsx` (11),
`NodeComparisonTable.tsx` (11), `report/page.tsx` (10), and the single-digit files
below them.

**Files:**
- Modify: each file listed above
- Modify: `website/scripts/check-no-literals.mjs`

- [ ] **Step 1: Get the current worklist**

```bash
cd website && grep -rcoE "#[0-9a-fA-F]{6}\b" components app --include="*.tsx" | grep -v ":0$" | sort -t: -k2 -rn
```

This prints remaining files by literal count. Work top down.

- [ ] **Step 2: Convert one file at a time**

For each file, apply the Task 6 Step 4 mapping table, then immediately append the
path to `GUARDED` in `check-no-literals.mjs` and run `npm run check:literals`. One
file per iteration — do not batch, because a wrong token mapping is far easier to
spot in a small diff.

- [ ] **Step 3: Verify the whole set**

```bash
cd website
npm run check:literals
npx --no-install tsc --noEmit
npm run build
grep -rcoE "#[0-9a-fA-F]{6}\b" components app --include="*.tsx" | grep -v ":0$" || echo "no literals remain"
```

Expected: guard passes, typecheck clean, build succeeds, and the final grep prints
"no literals remain".

- [ ] **Step 4: Commit**

```bash
git add website/components website/app website/scripts/check-no-literals.mjs
git commit -m "refactor(ui): move remaining components onto colour tokens

Every component colour now resolves through a token, so both themes work."
```

---

## Phase 3 — Restyle to the spec

### Task 9: Primitives

**Files:**
- Modify: `website/app/globals.css` (add the spec's primitive classes)
- Modify: `website/components/ui/button.tsx`
- Modify: `website/components/StatusBadge.tsx`

- [ ] **Step 1: Add the spec's primitive classes to globals.css**

Copy verbatim from the artifact: `.btn` and its variants (`.btn-primary`,
`.btn-ghost`, `.btn-outline`, `.btn-danger`, `.btn-sm`, `.btn-icon`), `.spinner`,
`.inp` + `.field-msg`, `.switch`, `.seg`, `.tabs` + `.tab-count`, `.chip` + all
`.chip-*` variants, `.stripe*`, `.kbd`, `.tip`, `.lnk`. These are pure CSS reading
tokens — no changes needed.

- [ ] **Step 2: Point the Button component at `.btn`**

`components/ui/button.tsx` uses `class-variance-authority`. Replace the variant class
strings so each maps to the spec class: `default` → `btn btn-primary`, `ghost` →
`btn btn-ghost`, `outline` → `btn btn-outline`, `destructive` → `btn btn-danger`,
and size `sm` → adds `btn-sm`, `icon` → adds `btn-icon`. Keep the existing prop API
so no call site changes.

- [ ] **Step 3: Point StatusBadge at `.chip`**

Map each of the 8 statuses to a chip variant: `pass` → `chip chip-ok`,
`crashed` → `chip chip-tool`, `fail`/`degraded_input` → `chip chip-quality`,
`semantic_fail` → `chip chip-semantic`, `interrupted` → `chip chip-coherence`,
`retried` → `chip chip-iris`, `skipped` → `chip chip-idle`. Render the `<span class="dot" />`
child the spec expects. For a run that is still executing, use `chip chip-run`,
whose dot carries the breathing animation.

- [ ] **Step 4: Verify**

```bash
cd website && npm run check:literals && npx --no-install tsc --noEmit && npm run build
cd .. && pytest tests/test_ui_parity.py -v
```

Then `npm run dev` and check the run list and a run detail page in both themes:
buttons are 32px tall with the spec's radii, status chips are pill-shaped with a
leading dot, and focus rings appear on keyboard tab.

- [ ] **Step 5: Commit**

```bash
git add website/app/globals.css website/components/ui/button.tsx website/components/StatusBadge.tsx
git commit -m "feat(ui): restyle buttons, inputs and status chips to the spec"
```

---

### Task 10: Data display

**Files:**
- Modify: `website/app/globals.css`
- Modify: `website/components/RunTable.tsx`, `website/components/RunListPanel.tsx`
- Modify: `website/components/run-detail/MetricsGrid.tsx`, `RunMetricsBar.tsx`, `ExecutionTimeline.tsx`
- Modify: `website/components/JsonViewer.tsx`, `website/components/CliLogViewer.tsx`
- Modify: `website/app/compare/components/StructuredDiff.tsx`

- [ ] **Step 1: Add the spec's data-display classes**

Copy from the artifact into `globals.css`: `.tile` + `.tile-*` + `.delta*`,
`.meter`, `.tbl` (including the `aria-selected` row treatment), `.kv-row`, `.json`
+ `.j-*`, `.diff` + `.add`/`.del`/`.ctx`, `.log` + `.log-*` + `.lv-*`, `.tline` +
`.tl-*`.

- [ ] **Step 2: Apply `.tile` and `.meter` to the metrics components**

`MetricsGrid.tsx` and `RunMetricsBar.tsx` render stat cards. Give each the `.tile`
structure: `.tile-top` (label + optional chip), `.tile-val` (mono, tabular numerals),
`.tile-foot` (delta + sub-label). Note the spec's `.delta-up` is red and `.delta-down`
is green — for a failure dashboard, "up" is bad. Verify each metric's direction
before assigning the class.

- [ ] **Step 3: Apply `.tbl` to the run tables**

`RunTable.tsx` and `RunListPanel.tsx` get `.tbl`. Selected rows use
`aria-selected="true"`, which the spec styles with an inset accent bar — remove any
existing bespoke selected-row styling so the two do not fight.

- [ ] **Step 4: Apply the payload viewers**

`JsonViewer.tsx` → `.json` with `.j-k`/`.j-s`/`.j-n`/`.j-b`; `CliLogViewer.tsx` →
`.log` with the 3-column `.log-line` grid and `.lv-*` level colours;
`StructuredDiff.tsx` → `.diff` with `.add`/`.del` gutters;
`ExecutionTimeline.tsx` → `.tline` with `.tl-dot` state variants.

- [ ] **Step 5: Verify**

```bash
cd website && npm run check:literals && npx --no-install tsc --noEmit && npm run build
```

Then in `npm run dev`, open a run with a crash and a run with a semantic failure.
Confirm in both themes: numbers are tabular and do not jitter, the diff gutters are
visible, and log levels are distinguishable.

- [ ] **Step 6: Commit**

```bash
git add website/app/globals.css website/components website/app/compare
git commit -m "feat(ui): restyle tables, tiles, json, diff, log and timeline to the spec"
```

---

### Task 11: Render the findings list (gap G1)

The highest-impact gap. PR #60 made `RunRecord.findings` the canonical normalized
failure list — `CLAUDE.md` states *"Consumers read this list, not the step shapes."*
It is typed at `lib/types.ts` with zero render sites, so the dashboard still
reconstructs failures by walking step shapes and can silently disagree with
`argus check` and `argus show`.

**Files:**
- Create: `website/components/FindingsPanel.tsx`
- Modify: `website/components/run-detail/OverviewTab.tsx`
- Modify: `website/app/globals.css`

- [ ] **Step 1: Add the spec's feedback classes**

Copy `.banner` + `.b-*` variants, `.rc` + `.rc-*` (root cause), `.toast`, `.empty`,
`.modal`, `.sk` (skeleton) from the artifact into `globals.css`.

- [ ] **Step 2: Write the findings panel**

```tsx
// website/components/FindingsPanel.tsx
'use client'

import type { Finding, RunRecord } from '@/lib/types'
import { getFailureMeta } from '@/lib/failure-labels'

const SEVERITY_CLASS: Record<Finding['severity'], string> = {
  critical: 'b-tool',
  warning: 'b-quality',
  info: 'b-coherence',
}

export default function FindingsPanel({ run }: { run: RunRecord }) {
  const findings = (run.findings ?? []).filter((f) => !f.suppressed)

  if (findings.length === 0) {
    return (
      <div className="banner b-ok">
        <div>
          <h4>No findings</h4>
          <p>Every node in this run passed inspection, validation and the semantic judge.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="rowv">
      {findings.map((finding) => {
        const meta = getFailureMeta(finding.type)
        return (
          <div key={finding.id} className={`banner ${SEVERITY_CLASS[finding.severity]}`}>
            <div style={{ minWidth: 0 }}>
              <h4>
                {meta.label} <span style={{ color: 'var(--ink-3)' }}>· {finding.node}</span>
              </h4>
              <p>{finding.reason}</p>
              <p className="rc-meta" style={{ marginTop: 6 }}>
                {finding.source}
                {finding.field_path ? ` · ${finding.field_path}` : ''}
                {finding.origin_node && finding.origin_node !== finding.node
                  ? ` · originated in ${finding.origin_node}`
                  : ''}
                {typeof finding.confidence === 'number'
                  ? ` · ${Math.round(finding.confidence * 100)}% confidence`
                  : ''}
              </p>
            </div>
          </div>
        )
      })}
    </div>
  )
}
```

`suppressed` findings are filtered out deliberately — the backend sets that flag when
a signal was overridden, and showing them would contradict `argus check`.

- [ ] **Step 3: Mount it as the primary failure view**

In `components/run-detail/OverviewTab.tsx`, render `<FindingsPanel run={run} />` above
the existing per-step failure summaries. Do not delete the step-derived views yet —
compare them side by side against `argus show <run-id>` output on a few real runs
first, then remove the step-derived duplication in a follow-up once you have
confirmed findings covers every case.

- [ ] **Step 4: Verify against the CLI**

```bash
cd /Users/varaddurge/Documents/ARGUS
argus list
argus show <a-failing-run-id>
cd website && npm run dev
```

Open the same run in the dashboard. Every failure the CLI reports must appear in the
findings panel, with the same node attribution. A mismatch is a real bug — findings
is generated by `findings.collect_findings()` and both surfaces read the same record.

- [ ] **Step 5: Commit**

```bash
git add website/components/FindingsPanel.tsx website/components/run-detail/OverviewTab.tsx website/app/globals.css
git commit -m "feat(ui): render RunRecord.findings as the canonical failure list

The dashboard previously rebuilt failures from step shapes and could disagree
with argus check. It now reads the same normalized list the CLI does."
```

---

### Task 12: Root cause panel and the CI verdict chip (gap G4)

`argus check` is the CI gate, but no run page answers "would this fail CI?".

**Files:**
- Create: `website/lib/check.ts`
- Modify: `website/components/run-detail/RootCauseBanner.tsx`
- Modify: `website/components/run-detail/RunHeader.tsx`

- [ ] **Step 1: Port the grading rule**

Mirror `src/argus/check.py:evaluate_run` — a run is clean when its overall status is
clean and no node contributed a failure reason. Read that file before writing this;
if the two disagree, `check.py` is authoritative.

```typescript
// website/lib/check.ts
import type { RunRecord } from './types'

export interface CheckVerdict {
  passed: boolean
  reasons: string[]
}

/** Client-side mirror of `argus check` (src/argus/check.py:evaluate_run). */
export function evaluateRun(run: RunRecord): CheckVerdict {
  const reasons: string[] = []

  if (run.overall_status !== 'clean') {
    reasons.push(`overall_status=${run.overall_status}`)
  }

  for (const finding of run.findings ?? []) {
    if (finding.suppressed || finding.severity !== 'critical') continue
    reasons.push(`${finding.node}: ${finding.type}`)
  }

  return { passed: reasons.length === 0, reasons }
}
```

- [ ] **Step 2: Add the verdict chip to the run header**

In `RunHeader.tsx`, call `evaluateRun(run)` and render a chip next to the run id:

```tsx
{verdict.passed ? (
  <span className="chip chip-ok" title="argus check would exit 0">
    <span className="dot" />CI clean
  </span>
) : (
  <span className="chip chip-tool" title={verdict.reasons.join('\n')}>
    <span className="dot" />CI fail · {verdict.reasons.length}
  </span>
)}
```

- [ ] **Step 3: Restyle the root cause banner**

Apply the spec's `.rc` structure to `RootCauseBanner.tsx`: `.rc-bar` header,
`.rc-chain` rendering `run.root_cause_chain` as `.rc-node` pills with `→` separators
and `.rc-node.culprit` on the blamed node, then `.rc-expl` for the explanation and
`.rc-meta` for the supporting detail.

- [ ] **Step 4: Verify the verdict matches the CLI exactly**

```bash
cd /Users/varaddurge/Documents/ARGUS
for id in $(argus list | awk 'NR>1 {print $1}' | head -5); do
  argus check "$id"; echo "  exit=$?"
done
```

Open each of those runs in the dashboard. A run where `argus check` exits 1 must show
"CI fail"; exit 0 must show "CI clean". Any disagreement means `check.ts` has drifted
from `check.py` — fix `check.ts`.

- [ ] **Step 5: Commit**

```bash
git add website/lib/check.ts website/components/run-detail/RootCauseBanner.tsx website/components/run-detail/RunHeader.tsx
git commit -m "feat(ui): show the argus check verdict and restyle the root cause chain"
```

---

### Task 13: Execution graph

**Files:**
- Modify: `website/components/run-detail/ExecutionGraph.tsx`
- Modify: `website/app/globals.css`

- [ ] **Step 1: Add the spec's graph classes**

Copy `.gwrap`, `.gbar`, `.gzoom`, `.gcanvas` (with its radial dot-grid background),
`.gnode` + `.gnode-*` + the `.s-*` state variants, `.gtick` into `globals.css`.

- [ ] **Step 2: Restyle the xyflow nodes**

Keep `@xyflow/react` — it already provides pan, zoom, and edge routing. Do **not**
hand-roll the pan/zoom from the spec's demo markup; only borrow the visual treatment.
Apply `.gnode` and the state class matching each node's status: `crashed` →
`s-crashed`, `fail`/`degraded_input` → `s-fail`, `semantic_fail` → `s-semantic`,
`pass` → default. Set the canvas background to the spec's dot-grid and style the
xyflow controls with `.gzoom`.

- [ ] **Step 3: Verify**

```bash
cd website && npm run check:literals && npx --no-install tsc --noEmit && npm run build
```

Then open a run with a crash. Confirm the crashed node carries the spec's red glow
ring, pan and zoom still work, and edges are visible in both themes.

- [ ] **Step 4: Commit**

```bash
git add website/components/run-detail/ExecutionGraph.tsx website/app/globals.css
git commit -m "feat(ui): restyle the execution graph nodes and canvas to the spec"
```

---

### Task 14: Shell and layout

**Files:**
- Modify: `website/app/globals.css`
- Modify: `website/components/Sidebar.tsx`, `website/components/Topbar.tsx`
- Modify: `website/app/layout.tsx`

- [ ] **Step 1: Apply the rail treatment to the sidebar**

The spec's rail is a 236px sticky column with a 1px right border, a mark + version
block, and nav rows using a `22px 1fr` grid. Apply it to `Sidebar.tsx`, keeping the
existing section grouping (Observe / Analyze / Workflows) and the `soon: true`
disabled treatment — use `--ink-4` for those rather than removing them.

Note: `html, body { overflow: hidden }` makes this an app shell, so the rail is
already non-scrolling with the page. Keep `overflow-y: auto` on the rail itself for
long nav lists.

- [ ] **Step 2: Apply section rhythm and focus states**

Add the spec's `::selection`, `:focus-visible`, and scrollbar rules. Skip the
marketing-oriented `.masthead` and `.rv` scroll-reveal classes — this is an app
shell, not a document, and a fade-in on every panel would fight the data-density the
rest of the spec is built for.

- [ ] **Step 3: Verify**

```bash
cd website && npm run check:literals && npx --no-install tsc --noEmit && npm run build
```

Then walk every route in both themes: `/`, `/runs/<id>`, `/compare`, `/approvals`,
`/report`, `/guide`, `/changelog`, `/settings`, `/login`. Check nothing is clipped at
a 1280px viewport and that keyboard tab order is visible throughout.

- [ ] **Step 4: Commit**

```bash
git add website/app website/components
git commit -m "feat(ui): apply the spec shell, rail and focus treatment"
```

---

## Phase 4 — Close out

### Task 15: Full verification and PR

- [ ] **Step 1: Run everything**

```bash
cd /Users/varaddurge/Documents/ARGUS
pytest tests/ -q
cd website
npm run check:literals
npx --no-install tsc --noEmit
npm run lint
npm run build
```

Expected: pytest green, guard passes, typecheck clean, lint clean, build succeeds.

- [ ] **Step 2: Confirm no literals survived**

```bash
cd website && grep -rcoE "#[0-9a-fA-F]{6}\b" components app --include="*.tsx" | grep -v ":0$" || echo "clean"
```

Expected: "clean".

- [ ] **Step 3: Update the docs**

Update `website/REDESIGN.md` to mark the phases done and record which gaps remain
open (G5 `argus stats` has no UI; the step-derived failure views left in place by
Task 11 Step 3). Add a line to `website/app/changelog/page.tsx` describing the
redesign, matching the existing entry format.

- [ ] **Step 4: Open the PR as a draft**

```bash
git push -u origin dashboard-redesign
gh pr create --draft --title "feat(ui): redesign the dashboard to the Argus Instrument spec" --body "$(cat <<'EOF'
## What

Restyles the dashboard to the Argus Instrument interface spec — Apple/macOS visual
language, dual light+dark theme, every colour resolved through a token.

Also closes six gaps where the UI had drifted behind the backend:

- Renders `RunRecord.findings` (#60) — previously typed but never displayed
- Labels `empty_output` (#36) and `json_in_string` (#34), which rendered as "Unknown"
- Labels the `chat_response` / `code_generation` behavior profiles (#65)
- Surfaces the `argus check` CI verdict on the run header (#61, #64)
- Mirrors `state_patch`, `coverage_summary`, `schema_version` onto the TS `RunRecord`

## Refactor

~340 hardcoded hex literals moved onto tokens — a literal cannot respond to a theme
change, so this was a prerequisite for light mode. `StepInspector.tsx` (795 lines) and
`approvals/page.tsx` (1027 lines) were split while being converted.

## Guards

- `tests/test_ui_parity.py` fails if a Python enum value has no UI label
- `npm run check:literals` fails if a colour literal returns to a converted file

## Test plan

- [ ] `pytest tests/` green
- [ ] `npm run check:literals`, `tsc --noEmit`, `npm run lint`, `npm run build` all clean
- [ ] Every route walked in light, dark, and system themes
- [ ] Dashboard findings match `argus show` on a crash, a silent failure and a semantic failure
- [ ] CI verdict chip matches `argus check` exit code across several runs

Still open: `argus stats` has no dashboard surface (G5).
EOF
)"
```

Per `docs/superpowers/plans` convention and prior instruction, the PR stays a **draft**
— do not mark it ready or merge without asking.

---

## Notes and deliberate omissions

- **No new dependencies.** xyflow, radix and lucide already cover the spec.
- **No test framework for the website.** We are not unit-testing CSS. The parity test
  lives in the existing pytest suite where CI already runs it.
- **Marketing chrome skipped** (`.masthead`, `.rv` scroll-reveal, `.mast-meta`). The
  artifact is a spec document that presents itself as a page; those classes style the
  document, not the product.
- **`argus stats` (G5) is out of scope.** It is a whole feature surface — signature
  effectiveness, dispute, prune, disable — and belongs in its own plan.
- **Step-derived failure views survive Task 11** until findings is verified against
  the CLI on real runs. Removing them is a follow-up, not a redesign task.
