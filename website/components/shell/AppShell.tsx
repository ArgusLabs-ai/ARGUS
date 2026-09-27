'use client'

/* The IDE frame: icon rail · run explorer · workspace, flush and
   edge-to-edge, divided by hairlines. Wraps every route. */

import type { ReactNode } from 'react'
import { WorkspaceProvider } from '@/lib/workspace'
import IconRail from './IconRail'
import RunExplorer from './RunExplorer'
import Workspace from './Workspace'

export default function AppShell({ children }: { children: ReactNode }) {
  /* No Suspense here: the provider keeps its own narrow boundary around the
     one component that reads search params, so this tree — and the prerendered
     HTML for the public /guide and /changelog pages — stays intact. */
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
