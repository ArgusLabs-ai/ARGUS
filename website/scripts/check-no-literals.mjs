// Fails if a restyled file reintroduces a hardcoded colour literal.
// Files graduate onto this list as Phase 2 de-hardcodes them.
import { readFileSync } from 'node:fs'

// Every component colour now resolves through a token. SendReportDialog is
// excluded: its modal scrim is deliberately a black rgba in both themes.
const GUARDED = [
  'app/approvals/ApprovalRows.tsx',
  'app/approvals/page.tsx',
  'app/approvals/types.ts',
  'app/changelog/layout.tsx',
  'app/changelog/page.tsx',
  'app/compare/CompareHeader.tsx',
  'app/compare/CompareTabNav.tsx',
  'app/compare/components/ChangeImpactChart.tsx',
  'app/compare/components/KeyChangesSummary.tsx',
  'app/compare/components/NodeComparisonTable.tsx',
  'app/compare/components/PipelineComparison.tsx',
  'app/compare/components/StructuredDiff.tsx',
  'app/compare/components/SummaryMetrics.tsx',
  'app/compare/layout.tsx',
  'app/compare/lib/compare-utils.ts',
  'app/compare/page.tsx',
  'app/compare/tabs/AIAnalysisTab.tsx',
  'app/compare/tabs/DiffViewTab.tsx',
  'app/compare/tabs/LogsTab.tsx',
  'app/compare/tabs/MetricsTab.tsx',
  'app/compare/tabs/NodeComparisonTab.tsx',
  'app/compare/tabs/OverviewTab.tsx',
  'app/compare/tabs/TimelineTab.tsx',
  'app/error.tsx',
  'app/guide/guide-content.tsx',
  'app/guide/layout.tsx',
  'app/guide/page.tsx',
  'app/layout.tsx',
  'app/page.tsx',
  'app/robots.ts',
  'app/runs/[id]/RunDetailClient.tsx',
  'app/runs/[id]/page.tsx',
  'app/settings/layout.tsx',
  'app/settings/page.tsx',
  'app/sitemap.ts',
  'components/CliLogViewer.tsx',
  'components/EmptyRunsState.tsx',
  'components/EvalBadge.tsx',
  'components/EvaluationBuilder.tsx',
  'components/JsonViewer.tsx',
  'components/RunDetailPanel.tsx',
  'components/RunDetailView.tsx',
  'components/RunListPanel.tsx',
  'components/RunTable.tsx',
  'components/Sidebar.tsx',
  'components/StatusBadge.tsx',
  'components/StepCard.tsx',
  'components/ThemeToggle.tsx',
  'components/Topbar.tsx',
  'components/run-detail/AIAnalysisPanel.tsx',
  'components/run-detail/BehaviorPanel.tsx',
  'components/run-detail/CorrelationPanel.tsx',
  'components/run-detail/CycleGroup.tsx',
  'components/run-detail/ExecutionGraph.tsx',
  'components/run-detail/ExecutionTimeline.tsx',
  'components/run-detail/MetricsGrid.tsx',
  'components/run-detail/OverviewTab.tsx',
  'components/run-detail/ParallelGroup.tsx',
  'components/run-detail/PipelineOverview.tsx',
  'components/run-detail/ReplayBranches.tsx',
  'components/run-detail/ReplayControls.tsx',
  'components/run-detail/RootCauseBanner.tsx',
  'components/run-detail/RunHeader.tsx',
  'components/run-detail/RunMetricsBar.tsx',
  'components/run-detail/StatusCard.tsx',
  'components/run-detail/StepInspector.tsx',
  'components/run-detail/StepInspectorSignals.tsx',
  'components/run-detail/StepRow.tsx',
  'components/ui/button-1.tsx',
  'components/ui/button.tsx',
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
