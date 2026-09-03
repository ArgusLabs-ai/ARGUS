export type Theme = 'light' | 'dark' | 'system'

const KEY = 'argus-theme'

export function getStoredTheme(): Theme {
  if (typeof window === 'undefined') return 'system'
  const raw = window.localStorage.getItem(KEY)
  return raw === 'light' || raw === 'dark' ? raw : 'system'
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement
  if (theme === 'system') {
    root.removeAttribute('data-theme')
    window.localStorage.removeItem(KEY)
  } else {
    root.setAttribute('data-theme', theme)
    window.localStorage.setItem(KEY, theme)
  }
}
