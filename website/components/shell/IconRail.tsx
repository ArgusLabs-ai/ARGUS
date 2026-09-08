'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useEffect, useState } from 'react'
import {
  Activity,
  GitCompareArrows,
  ClipboardCheck,
  Database,
  Network,
  BookOpen,
  Clock,
  Settings,
  Sun,
  Moon,
} from 'lucide-react'
import { useMaintainerPreview } from '@/lib/preview'
import { getStoredTheme, applyTheme as setTheme, type Theme } from '@/lib/theme'
import { useWorkspace } from '@/lib/workspace'
import { cn } from '@/lib/utils'

interface RailItem {
  id: string
  href: string
  title: string
  icon: React.ReactNode
  exact?: boolean
  soon?: boolean
}

const top: RailItem[] = [
  { id: 'runs', href: '/', title: 'Runs', icon: <Activity />, exact: true },
  { id: 'compare', href: '/compare', title: 'Compare', icon: <GitCompareArrows /> },
  { id: 'approvals', href: '/approvals', title: 'Approvals', icon: <ClipboardCheck /> },
]

const planned: RailItem[] = [
  { id: 'datasets', href: '/datasets', title: 'Datasets · planned', icon: <Database />, soon: true },
  { id: 'graphs', href: '/graphs', title: 'Graphs · planned', icon: <Network />, soon: true },
]

const bottom: RailItem[] = [
  { id: 'guide', href: '/guide', title: 'Guide', icon: <BookOpen />, exact: true },
  { id: 'changelog', href: '/changelog', title: 'Changelog', icon: <Clock />, exact: true },
  { id: 'settings', href: '/settings', title: 'Settings', icon: <Settings /> },
]

function ThemeButton() {
  const [theme, setLocal] = useState<Theme>('system')
  const [dark, setDark] = useState(false)

  useEffect(() => {
    setLocal(getStoredTheme())
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const read = () => {
      const t = document.documentElement.getAttribute('data-theme')
      setDark(t === 'dark' || (!t && mq.matches))
    }
    read()
    mq.addEventListener('change', read)
    return () => mq.removeEventListener('change', read)
  }, [theme])

  const toggle = () => {
    const next: Theme = dark ? 'light' : 'dark'
    setTheme(next)
    setLocal(next)
  }

  return (
    <button type="button" title={dark ? 'Switch to light' : 'Switch to dark'} onClick={toggle} aria-label="Toggle theme">
      {dark ? <Sun /> : <Moon />}
    </button>
  )
}

export default function IconRail() {
  const pathname = usePathname()
  const preview = useMaintainerPreview()
  const { goHome, activeRunId } = useWorkspace()

  const isOn = (item: RailItem) =>
    item.exact ? pathname === item.href : pathname.startsWith(item.href)

  const render = (item: RailItem) => {
    if (item.soon) {
      return (
        <a key={item.id} className="is-soon" title={item.title} aria-disabled="true">
          {item.icon}
        </a>
      )
    }
    if (item.id === 'runs') {
      /* Runs returns to the list even when a run tab is active. */
      return (
        <button
          key={item.id}
          type="button"
          title={activeRunId ? 'All runs' : 'Runs'}
          className={cn(isOn(item) && 'on')}
          onClick={goHome}
          aria-current={isOn(item) ? 'page' : undefined}
        >
          {item.icon}
        </button>
      )
    }
    return (
      <Link key={item.id} href={item.href} title={item.title} className={cn(isOn(item) && 'on')} aria-current={isOn(item) ? 'page' : undefined}>
        {item.icon}
      </Link>
    )
  }

  return (
    <nav className="irail" aria-label="Primary">
      <span className="irail-mark" aria-hidden="true">
        <svg viewBox="0 0 18 18" fill="none">
          <path d="M9 1.5 16.5 5.5v7L9 16.5 1.5 12.5v-7L9 1.5Z" stroke="currentColor" strokeWidth="1.4" />
          <circle cx="9" cy="9" r="2.2" stroke="currentColor" strokeWidth="1.3" />
          <circle cx="9" cy="9" r=".9" fill="currentColor" />
        </svg>
      </span>
      {top.map(render)}
      {preview && planned.map(render)}
      <span className="sp" />
      {bottom.map(render)}
      <ThemeButton />
    </nav>
  )
}
