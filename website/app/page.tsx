'use client'

import { Suspense, useEffect, useMemo, useState } from 'react'
import { useSearchParams, useRouter } from 'next/navigation'
import { useRunList } from '@/lib/hooks'
import type { RunStatus } from '@/lib/types'
import RunListPanel from '@/components/RunListPanel'
import RunDetailPanel from '@/components/RunDetailPanel'

function RunListPageInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const selectedRunId = searchParams.get('run')
  const previousRunId = searchParams.get('from')
  const { runs, loading } = useRunList()

  const handleSelectRun = (id: string) => {
    router.replace(`/?run=${id}`, { scroll: false })
  }

  const handleCloseDetail = () => {
    router.replace('/', { scroll: false })
  }

  if (loading && !runs.length) {
    return (
      <div className="h-full flex items-center justify-center">
        <span className="text-sm text-muted-foreground">Loading...</span>
      </div>
    )
  }

  /* Full-width toggle: list OR detail */
  if (selectedRunId) {
    return (
      <RunDetailPanel
        runId={selectedRunId}
        previousRunId={previousRunId}
        onClose={handleCloseDetail}
        allRuns={runs}
      />
    )
  }

  return (
    <RunListPanel
      runs={runs}
      selectedRunId={selectedRunId}
      onSelectRun={handleSelectRun}
      loading={loading}
    />
  )
}

export default function RunListPage() {
  return (
    <Suspense
      fallback={
        <div className="h-full flex items-center justify-center">
          <span className="text-sm text-muted-foreground">Loading...</span>
        </div>
      }
    >
      <RunListPageInner />
    </Suspense>
  )
}
