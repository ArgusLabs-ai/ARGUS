import type { MetadataRoute } from 'next'

const BASE = 'https://arguslabs.in'

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: BASE, lastModified: new Date(), changeFrequency: 'weekly', priority: 1.0 },
    { url: `${BASE}/guide`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.8 },
    { url: `${BASE}/changelog`, lastModified: new Date(), changeFrequency: 'weekly', priority: 0.7 },
    { url: `${BASE}/compare`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.5 },
  ]
}
