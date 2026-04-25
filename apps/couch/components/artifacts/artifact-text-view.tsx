'use client'

import { useQuery } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function ArtifactTextView({ artifactId }: { artifactId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['artifact-content', artifactId],
    queryFn: async () => {
      const res = await fetch(`/api/ahs/artifact/${artifactId}/content`)
      if (!res.ok) throw new Error(`Status ${res.status}`)
      return res.text()
    },
    staleTime: Number.POSITIVE_INFINITY,
  })

  if (isLoading)
    return <p className="p-4 text-muted-foreground text-sm">Loading…</p>
  if (error)
    return <p className="p-4 text-destructive text-sm">Failed to load.</p>

  return (
    <div className="prose prose-sm max-w-none p-4">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{data ?? ''}</ReactMarkdown>
    </div>
  )
}
