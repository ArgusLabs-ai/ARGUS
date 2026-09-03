import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        background:   'var(--background)',
        foreground:   'var(--foreground)',
        card:         { DEFAULT: 'var(--card)',      foreground: 'var(--card-foreground)' },
        popover:      { DEFAULT: 'var(--popover)',   foreground: 'var(--popover-foreground)' },
        primary:      { DEFAULT: 'var(--primary)',   foreground: 'var(--primary-foreground)' },
        secondary:    { DEFAULT: 'var(--secondary)',  foreground: 'var(--secondary-foreground)' },
        muted:        { DEFAULT: 'var(--muted)',     foreground: 'var(--muted-foreground)' },
        accent:       { DEFAULT: 'var(--accent)',    foreground: 'var(--accent-foreground)' },
        destructive:  { DEFAULT: 'var(--destructive)', foreground: 'var(--destructive-foreground)' },
        success:      { DEFAULT: 'var(--success)',   foreground: 'var(--success-foreground)' },
        warning:      { DEFAULT: 'var(--warning)',   foreground: 'var(--warning-foreground)' },
        border:       'var(--border)',
        input:        'var(--input)',
        ring:         'var(--ring)',
        code:         { DEFAULT: 'var(--code-bg)',   header: 'var(--code-header)' },
        indigo:       'var(--primary)',
        'text-secondary': 'var(--text-secondary)',
        'text-tertiary':  'var(--text-tertiary)',
        sidebar: {
          DEFAULT:    'var(--sidebar)',
          foreground: 'var(--sidebar-foreground)',
          primary:    'var(--sidebar-primary)',
          'primary-foreground': 'var(--sidebar-primary-foreground)',
          accent:     'var(--sidebar-accent)',
          'accent-foreground': 'var(--sidebar-accent-foreground)',
          border:     'var(--sidebar-border)',
          ring:       'var(--sidebar-ring)',
        },
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) * 0.8)',
        sm: 'calc(var(--radius) * 0.6)',
        xl: 'calc(var(--radius) * 1.4)',
        '2xl': 'calc(var(--radius) * 1.8)',
      },
      fontFamily: {
        sans: ['var(--sans)'],
        mono: ['var(--mono)'],
      },
    },
  },
  plugins: [],
}

export default config
