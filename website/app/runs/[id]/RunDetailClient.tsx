'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

/**
 * Static export bakes params.id="_" (the generateStaticParams placeholder)
 * into this page, so the id prop is useless at runtime. Read the real id
 * from the browser URL instead, then hand off to the home page's ?run= view.
 */
export default function RunDetailRedirectClient({ id }: { id: string }) {
  const router = useRouter()

  useEffect(() => {
    const fromPath = window.location.pathname.match(/\/runs\/([^/]+)\/?$/)?.[1]
    const target = fromPath && fromPath !== '_' ? fromPath : id
    if (target && target !== '_') {
      router.replace(`/?run=${encodeURIComponent(target)}`)
    } else {
      router.replace('/')
    }
  }, [id, router])

  return (
    <div className="py-24 text-center text-sm" style={{ color: 'var(--text-muted)' }}>
      Redirecting...
    </div>
  )
}
