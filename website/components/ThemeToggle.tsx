'use client'

import { useEffect, useState } from 'react'
import { applyTheme, getStoredTheme, type Theme } from '@/lib/theme'

const OPTIONS: Theme[] = ['light', 'dark', 'system']

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>('system')

  useEffect(() => setTheme(getStoredTheme()), [])

  function choose(next: Theme) {
    setTheme(next)
    applyTheme(next)
  }

  return (
    <div className="seg" role="group" aria-label="Colour theme">
      {OPTIONS.map((option) => (
        <button
          key={option}
          type="button"
          aria-selected={theme === option}
          onClick={() => choose(option)}
        >
          {option[0].toUpperCase() + option.slice(1)}
        </button>
      ))}
    </div>
  )
}
