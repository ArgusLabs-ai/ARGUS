/* Fetch the same markdown `argus fix` prints, from the local UI server. */

export interface FixPromptPayload {
  run_id: string
  node: string
  source_path: string | null
  prompt: string
}

export async function fetchFixPrompt(runId: string, node?: string | null): Promise<FixPromptPayload> {
  const qs = node ? `?node=${encodeURIComponent(node)}` : ''
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/fix${qs}`, { cache: 'no-store' })
  const body = await res.json().catch(() => ({})) as { error?: string } & Partial<FixPromptPayload>
  if (!res.ok || !body.prompt || !body.node) {
    throw new Error(body.error || `Could not build a fix prompt (${res.status})`)
  }
  return {
    run_id: body.run_id ?? runId,
    node: body.node,
    source_path: body.source_path ?? null,
    prompt: body.prompt,
  }
}
