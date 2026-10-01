/* Plain-English root cause. Detectors speak in signature codes and field
   paths ("[CM-008] String ends with base64 padding pattern"); this turns the
   headline finding, the blame path and the crash into two sentences a person
   can act on: what the step did wrong, and what that led to. Backticked
   names render as code through <Prose>. The raw reason stays available as
   technical detail. */

import type { Finding, RunRecord } from './types'

/* Signature id prefix → signature category (src/argus/data/signatures.json). */
const SIG_KIND: Record<string, string> = {
  CM: 'corrupted_markers', ES: 'empty_semantic_state', MP: 'malformed_payload',
  NL: 'null_like_semantic', PH: 'placeholder_outputs', RF: 'repeated_filler',
  SP: 'suspicious_phrases', SS: 'semantic_refusal',
}

const code = (s: string) => `\`${s}\``

function list(nodes: string[]): string {
  const c = nodes.map(code)
  return c.length <= 1 ? c.join('') : `${c.slice(0, -1).join(', ')} and ${c[c.length - 1]}`
}

/** A degradation names its signature in the reason ("[CM-008]", "matched
    CM-008"); that is a sharper description than the generic type. */
function kindOf(f: Finding): string {
  const m = f.reason.match(/\b([A-Z]{2})-\d{3}\b/)
  if (m && SIG_KIND[m[1]] && f.source !== 'anomaly') return SIG_KIND[m[1]]
  if (f.source === 'anomaly') return 'anomaly'
  if (f.source === 'llm') return 'judge'
  return f.type
}

/** `open_tickets.[0].user.node_id` → `user.node_id`; internal `_x` fields → null. */
function fieldName(path: string | null | undefined): string | null {
  if (!path || path.startsWith('_')) return null
  const segs = path.split('.').filter((s) => s && !/^\[\d+\]$/.test(s))
  return segs.length ? segs.slice(-2).join('.') : null
}

/** The offending value when the reason quotes one at its end. */
function quotedValue(reason: string): string | null {
  const m = reason.match(/:\s*'([^']{1,40})'\.?\s*$/)
  return m ? m[1] : null
}

/** Last traceback line, said plainly where we can. */
export function plainError(exception: string | null | undefined): string | null {
  const last = exception?.split('\n').map((l) => l.trim()).filter(Boolean).pop()
  if (!last) return null
  const key = last.match(/^KeyError: '([^']+)'/)
  if (key) return `${code(key[1])} was missing`
  if (/NoneType/.test(last)) return 'it got an empty value (None) where it expected data'
  return `it raised ${code(last.length > 80 ? `${last.slice(0, 79)}…` : last)}`
}

/** What the step did wrong, as a predicate for "`node` …". */
function whatWentWrong(f: Finding, run: RunRecord): string {
  const field = fieldName(f.field_path)
  const target = field ? code(field) : 'its output'
  const at = field ? ` in ${code(field)}` : ''
  const value = quotedValue(f.reason)

  switch (kindOf(f)) {
    case 'crash': {
      const step = (run.steps ?? []).find((s) => s.node_name === f.node)
      const why = plainError(step?.exception)
      return why ? `crashed because ${why}` : 'crashed'
    }
    case 'corrupted_markers':
      return `returned a garbled value${at}${value ? ` (${code(value)})` : ''} that looks like encoded text, not real data`
    case 'null_like_semantic':
      return `put the text ${code(value ?? 'None')}${at} instead of a real value`
    case 'placeholder_outputs': case 'placeholder_detected':
      return `filled ${target} with a placeholder instead of real data`
    case 'repeated_filler':
      return `returned repetitive filler text${at} instead of real content`
    case 'suspicious_phrases': case 'semantic_refusal':
      return `got a refusal or canned reply${at} instead of an answer`
    case 'empty_semantic_state':
      return `returned an empty-looking value${at}`
    case 'malformed_payload':
      return `returned malformed data${at}`
    case 'semantic_degradation':
      return `returned low-quality data${at}`
    case 'empty_output':
      return 'returned nothing at all (an empty update)'
    case 'empty_result':
      return `got an empty result back${at}`
    case 'rate_limit':
      return `was rate-limited by a tool${at} but carried on as if it had worked`
    case 'error_response': case 'error_in_data': case 'partial_failure':
      return `got an error back from a tool${at} but carried on as if it had worked`
    case 'missing_field': {
      const reader = f.reason.match(/read by `([^`]+)`/)?.[1]
      const name = f.reason.match(/^Field `([^`]+)`/)?.[1] ?? field
      return `never produced ${name ? code(name) : 'a field'}${reader ? `, which ${code(reader)} needs` : ', which a later step needs'}`
    }
    case 'truncated_output':
      return `returned a cut-off value${at}`
    case 'json_in_string':
      return `returned ${target} as a JSON string instead of structured data`
    case 'shallow_output': case 'information_compression_anomaly':
      return `returned a suspiciously short answer${at}`
    case 'retrieval_quality_low': case 'shallow_context':
      return 'retrieved too little to work with'
    case 'input_echo':
      return 'just echoed its input back'
    case 'semantic_contradiction':
      return 'returned something that contradicts its input'
    case 'context_size_anomaly':
      return 'was handed an unusually large input, likely more context than it can use'
    case 'timeout_adjacent':
      return 'came close to timing out'
    case 'suspiciously_fast':
      return 'finished suspiciously fast for the work it does'
    case 'latency_quality_mismatch':
      return 'finished fast but returned poor output'
    case 'unused_result':
      return 'produced output the next step never used'
    case 'anomaly': {
      const what = f.reason.match(/unexpectedly:\s*(.+?)\.?$/)?.[1]
      return `behaved differently from its normal runs${what ? ` (${what})` : ''}`
    }
    case 'judge':
      return 'gave an answer the AI reviewer judged wrong'
    default:
      return `was flagged${at} (${f.type.replace(/_/g, ' ')})`
  }
}

/** Two sentences: what went wrong at the origin, and what it led to. */
export function explainRootCause(
  run: RunRecord,
  head: Finding,
  path: string[],
): { summary: string; impact: string | null } {
  const steps = run.steps ?? []
  const subject = head.node
  const origin = path[0] ?? subject
  const summary = `${code(subject)} ${whatWentWrong(head, run)}.`

  if (run.overall_status === 'clean') {
    return { summary, impact: 'The run still passed. This is a warning worth checking, not a failure.' }
  }

  /* The finding sits on a later step than the blamed one (a crash whose
     missing field an upstream step never wrote). Point back to the origin. */
  if (origin !== subject) {
    return { summary, impact: `The problem starts earlier, at ${code(origin)}, the step that should have provided the data.` }
  }

  const originStep = steps.find((s) => s.node_name === origin)
  if (originStep?.status === 'crashed') return { summary, impact: 'The run stopped there.' }

  const surfaced = path.length > 1 ? path[path.length - 1] : null
  const between = path.slice(1, -1)
  const crashStep = surfaced ? steps.find((s) => s.node_name === surfaced && s.status === 'crashed') : undefined

  if (crashStep) {
    const why = plainError(crashStep.exception)
    return {
      summary,
      impact: `It didn't raise an error, so the bad data ${between.length ? `passed through ${list(between)}` : 'moved on'} `
        + `and the run crashed at ${code(surfaced!)}${why ? ` because ${why}` : ''}.`,
    }
  }
  if (surfaced) {
    return { summary, impact: `It didn't raise an error, so the problem carried on into ${list(path.slice(1))}.` }
  }
  return { summary, impact: "It didn't raise an error, so the run finished with bad data." }
}
