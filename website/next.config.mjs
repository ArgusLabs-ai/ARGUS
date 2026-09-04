
const isDev = process.env.NODE_ENV === 'development'

const nextConfig = {
  reactStrictMode: true,
  // Static export for the bundled `argus ui` dist. Skip it in `next dev`
  // so /api rewrites to the Python server actually run.
  ...(process.env.VERCEL || isDev ? {} : { output: 'export', trailingSlash: true }),
  images: {
    unoptimized: true,
    remotePatterns: [
      { protocol: 'https', hostname: '*.googleusercontent.com' },
    ],
  },
  // In dev, proxy /api/* to the running argus Python server (port 7842)
  // Rewrites are ignored during static export builds
  ...(isDev ? {
    async rewrites() {
      return [
        { source: '/api/:path*', destination: 'http://localhost:7842/api/:path*' },
      ]
    },
  } : {}),
}

export default nextConfig
