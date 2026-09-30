'use client'

import { useWorkspace } from './workspace'

/** Maintainer preview: `?preview=1` restores planned nav that is hidden by default (US-4.1).
    Reads the workspace's query string, so client-side navigation (`Link`,
    `router.push`) flips it — the old `popstate` listener never fired for those,
    so the flag neither turned on when navigating to `?preview=1` nor cleared
    when leaving it. */
export function useMaintainerPreview(): boolean {
  return useWorkspace().query.get('preview') === '1'
}
