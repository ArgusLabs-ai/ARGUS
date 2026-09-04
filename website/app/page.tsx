'use client'

import { Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import { useWorkspace } from '@/lib/workspace'
import RunListPanel from '@/components/RunListPanel'
import RunDetailPanel from '@/components/RunDetailPanel'

function RunsPageInner() {
  const searchParams = useSearchParams()
  const { runs, runsLoading, activeRunId, goHome } = useWorkspace()
  const previousRunId = searchParams.get('from')

  if (activeRunId) {
    return (
      <RunDetailPanel
        key={activeRunId}
        runId={activeRunId}
        previousRunId={previousRunId}
        onClose={goHome}
        allRuns={runs}
      />
    )
  }

  return <RunListPanel runs={runs} loading={runsLoading} />
}

export default function RunsPage() {
  return (
    <Suspense fallback={null}>
      <RunsPageInner />
    </Suspense>
  )
}
