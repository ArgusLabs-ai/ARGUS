'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import ThemeToggle from '@/components/ThemeToggle'
import { cn } from '@/lib/utils'
import {
  Activity,
  Workflow,
  GitCompareArrows,
  ClipboardCheck,
  FlaskConical,
  Network,
  Bell,
  Database,
  BookOpen,
  Clock,
  Settings,
  ChevronsUpDown,
  Search,
} from 'lucide-react'

interface NavItem {
  id: string
  href: string
  label: string
  icon: React.ReactNode
  exact: boolean
  soon?: boolean
  badge?: string
}

interface NavSection {
  label: string
  items: NavItem[]
}

const navSections: NavSection[] = [
  {
    label: 'Observe',
    items: [
      { id: 'runs', href: '/', label: 'Runs', icon: <Activity className="h-4 w-4" />, exact: true },
      { id: 'traces', href: '/traces', label: 'Traces', icon: <Workflow className="h-4 w-4" />, exact: false, soon: true },
    ],
  },
  {
    label: 'Analyze',
    items: [
      { id: 'compare', href: '/compare', label: 'Compare', icon: <GitCompareArrows className="h-4 w-4" />, exact: false },
      { id: 'approvals', href: '/approvals', label: 'Approvals', icon: <ClipboardCheck className="h-4 w-4" />, exact: true },
      { id: 'evaluation', href: '/evaluation', label: 'Evaluation', icon: <FlaskConical className="h-4 w-4" />, exact: false, soon: true },
    ],
  },
  {
    label: 'Workflows',
    items: [
      { id: 'graphs', href: '/graphs', label: 'Graphs', icon: <Network className="h-4 w-4" />, exact: false, soon: true },
      { id: 'alerts', href: '/alerts', label: 'Alerts', icon: <Bell className="h-4 w-4" />, exact: false, soon: true },
      { id: 'datasets', href: '/datasets', label: 'Datasets', icon: <Database className="h-4 w-4" />, exact: false, soon: true },
    ],
  },
]

const bottomItems: NavItem[] = [
  { id: 'guide', href: '/guide', label: 'Guide', icon: <BookOpen className="h-4 w-4" />, exact: true },
  { id: 'changelog', href: '/changelog', label: 'Changelog', icon: <Clock className="h-4 w-4" />, exact: true },
  { id: 'settings', href: '/settings', label: 'Settings', icon: <Settings className="h-4 w-4" />, exact: false },
]

function NavRow({
  item,
  active,
}: {
  item: NavItem
  active: boolean
}) {
  const content = (
    <>
      <span className="shrink-0" style={{ color: active ? 'var(--iris)' : 'var(--ink-4)' }}>
        {item.icon}
      </span>
      <span className="flex min-w-0 flex-1 items-center text-left">
        <span className={cn('truncate', item.soon && !active && 'text-muted-foreground/70')}>
          {item.label}
        </span>
      </span>
      {item.soon && (
        <span className="chip chip-idle shrink-0 !h-[18px] !px-[6px] !text-[10px]">soon</span>
      )}
      {item.badge && (
        <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
          {item.badge}
        </span>
      )}
    </>
  )

  const rowClass = cn(
    // spec .rail a — 22px icon column, --r-chip radius, 13px label
    'grid w-full items-center gap-1 px-[9px] py-[6px] text-[13px] transition-colors',
    'grid-cols-[22px_1fr] rounded-[var(--r-chip)]',
    active ? 'font-medium' : '',
    item.soon && 'cursor-not-allowed select-none',
  )

  const rowStyle = active
    ? { background: 'var(--iris-dim)', color: 'var(--ink)' }
    : { color: item.soon ? 'var(--ink-4)' : 'var(--ink-2)' }

  if (item.soon) {
    return (
      <div className={rowClass} style={rowStyle} aria-disabled="true">
        {content}
      </div>
    )
  }

  return (
    <Link href={item.href} className={cn(rowClass, 'hover:bg-[var(--hover)]')} style={rowStyle}>
      {content}
    </Link>
  )
}

export default function Sidebar() {
  const pathname = usePathname()



  function isActive(item: NavItem) {
    if (item.soon) return false
    return item.exact ? pathname === item.href : pathname.startsWith(item.href)
  }

  return (
    <aside className="flex h-full w-[236px] shrink-0 flex-col" style={{ borderRight: "1px solid var(--line)", background: "var(--rail)" }}>
      {/* Workspace / brand */}
      <div className="flex items-center gap-2.5 px-4 pt-5 pb-1">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/15 ring-1 ring-primary/30">
          <svg width="14" height="14" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path
              d="M9 1.5L16.5 5.5V12.5L9 16.5L1.5 12.5V5.5L9 1.5Z"
              stroke="currentColor"
              strokeWidth="1.2"
              fill="none"
              className="text-primary"
            />
            <circle cx="9" cy="9" r="2.2" fill="currentColor" className="text-primary/15" stroke="currentColor" strokeWidth="1.1" />
            <circle cx="9" cy="9" r="0.9" fill="currentColor" className="text-primary" />
          </svg>
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[14px] font-semibold text-foreground" style={{ letterSpacing: "0.02em" }}>ARGUS</p>
          <p className="truncate text-[11.5px]" style={{ color: "var(--ink-3)" }}>Local</p>
        </div>
        <ChevronsUpDown className="h-4 w-4 shrink-0 text-muted-foreground" />
      </div>

      {/* Search */}
      <div className="px-3 pb-2">
        <button
          type="button"
          className="flex w-full items-center gap-2 rounded-md border border-border bg-input/40 px-2.5 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:border-ring/40"
        >
          <Search className="h-3.5 w-3.5 shrink-0" />
          <span className="flex-1">Search runs...</span>
          <kbd className="rounded border border-border bg-muted px-1.5 font-mono text-[10px] text-muted-foreground">
            /
          </kbd>
        </button>
      </div>

      {/* Main nav */}
      <nav className="flex-1 overflow-y-auto px-2 py-2">
        {navSections.map((section) => (
          <div key={section.label} className="mb-4">
            <p className="px-2 pb-1.5 text-[11px] font-semibold uppercase" style={{ letterSpacing: "0.09em", color: "var(--ink-4)" }}>
              {section.label}
            </p>
            <ul className="flex flex-col gap-0.5">
              {section.items.map((item) => (
                <li key={item.id}>
                  <NavRow item={item} active={isActive(item)} />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      {/* Bottom nav */}
      <div className="px-2 pb-2">
        <ul className="flex flex-col gap-0.5">
          {bottomItems.map((item) => (
            <li key={item.id}>
              <NavRow item={item} active={isActive(item)} />
            </li>
          ))}
        </ul>
      </div>

      {/* Theme */}
      <div className="px-3 py-2.5" style={{ borderTop: "1px solid var(--line)" }}>
        <ThemeToggle />
      </div>

      {/* Local mode indicator — this dashboard reads .argus/runs via `argus ui`. */}
      <div className="px-3 py-2.5" style={{ borderTop: "1px solid var(--line)" }}>
        <span className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: 'var(--ok)' }} />
          Local
        </span>
      </div>
    </aside>
  )
}
