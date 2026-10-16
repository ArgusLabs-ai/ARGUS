import type { Metadata } from 'next'
import Gate from '@/components/Gate'
import './globals.css'

export const metadata: Metadata = {
  title: 'Argus Admin',
  description: 'Private telemetry panel for the Argus maintainers.',
  robots: { index: false, follow: false },
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Gate>{children}</Gate>
      </body>
    </html>
  )
}
