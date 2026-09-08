'use client'

import { useEffect, useState } from 'react'

/** Maintainer preview: `?preview=1` restores planned nav that is hidden by default (US-4.1). */
export function useMaintainerPreview(): boolean {
  const [enabled, setEnabled] = useState(false)

  useEffect(() => {
    const read = () => {
      try {
        setEnabled(new URLSearchParams(window.location.search).get('preview') === '1')
      } catch {
        setEnabled(false)
      }
    }
    read()
    window.addEventListener('popstate', read)
    return () => window.removeEventListener('popstate', read)
  }, [])

  return enabled
}
