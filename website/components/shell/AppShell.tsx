'use client'

/* The IDE frame: icon rail · run explorer · workspace, flush and
   edge-to-edge, divided by hairlines. Wraps every route. */

import { Suspense, type ReactNode } from 'react'
import { WorkspaceProvider } from '@/lib/workspace'
import IconRail from './IconRail'
import RunExplorer from './RunExplorer'
import Workspace from './Workspace'

function Frame({ children }: { children: ReactNode }) {
  return (
    <WorkspaceProvider>
      <div className="ide app">
        <IconRail />
        <RunExplorer />
        <Workspace>{children}</Workspace>
      </div>
    </WorkspaceProvider>
  )
}

export default function AppShell({ children }: { children: ReactNode }) {
  /* useSearchParams inside the provider requires a Suspense boundary. */
  return (
    <Suspense fallback={<div className="ide app" />}>
      <Frame>{children}</Frame>
    </Suspense>
  )
}
