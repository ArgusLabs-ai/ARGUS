import type { Metadata } from 'next'
import Sidebar from '@/components/Sidebar'
import { AuthProvider } from '@/lib/auth'
import './globals.css'

// The interface uses the system stacks defined by --sans / --mono in globals.css
// (SF Pro / SF Mono per the spec), so no webfont is loaded.

const SITE_URL = 'https://arguslabs.in'
const SITE_TITLE = 'ARGUS — Production Readiness for AI Agent Pipelines'
const SITE_DESC =
  'Detect silent failures, semantic degradation, and contract violations in your AI agent pipelines before deployment. LangGraph-first, framework-agnostic.'

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: SITE_TITLE,
    template: '%s | ARGUS',
  },
  description: SITE_DESC,
  keywords: [
    'AI agent monitoring',
    'LangGraph debugging',
    'AI pipeline testing',
    'silent failure detection',
    'LLM observability',
    'agent pipeline reliability',
    'semantic degradation',
    'AI production readiness',
    'LangChain monitoring',
    'AI agent debugging',
  ],
  authors: [{ name: 'ARGUS Labs' }],
  creator: 'ARGUS Labs',
  openGraph: {
    type: 'website',
    locale: 'en_US',
    url: SITE_URL,
    siteName: 'ARGUS',
    title: SITE_TITLE,
    description: SITE_DESC,
  },
  twitter: {
    card: 'summary_large_image',
    title: SITE_TITLE,
    description: SITE_DESC,
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      'max-video-preview': -1,
      'max-image-preview': 'large',
      'max-snippet': -1,
    },
  },
  alternates: {
    canonical: SITE_URL,
  },
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/* Applies a stored theme before first paint so there is no flash of the
            OS theme on reload. Must be inline and synchronous. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('argus-theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t)}catch(e){}})()`,
          }}
        />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@graph': [
                {
                  '@type': 'Organization',
                  name: 'ARGUS Labs',
                  url: SITE_URL,
                  description: SITE_DESC,
                },
                {
                  '@type': 'WebSite',
                  name: 'ARGUS',
                  url: SITE_URL,
                  description: SITE_DESC,
                  publisher: { '@type': 'Organization', name: 'ARGUS Labs' },
                },
                {
                  '@type': 'SoftwareApplication',
                  name: 'ARGUS',
                  applicationCategory: 'DeveloperApplication',
                  operatingSystem: 'Cross-platform',
                  description: SITE_DESC,
                  url: SITE_URL,
                  offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
                },
              ],
            }),
          }}
        />
      </head>
      <body className="h-screen overflow-hidden bg-background font-sans text-foreground antialiased flex">
        <AuthProvider>
          <Sidebar />
          <main className="flex-1 flex flex-col h-full overflow-hidden min-w-0">
            {children}
          </main>
        </AuthProvider>
      </body>
    </html>
  )
}
