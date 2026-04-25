'use client'

import { ExternalLink } from 'lucide-react'
import type { AhsArtifact } from '@/lib/ahs/server/client'

function extractPrUrl(artifact: AhsArtifact): string | null {
  const meta = artifact.metadata as Record<string, unknown> | null | undefined
  const candidate = meta?.url ?? meta?.pr_url ?? meta?.html_url
  return typeof candidate === 'string' ? candidate : null
}

export function ArtifactPrView({ artifact }: { artifact: AhsArtifact }) {
  const url = extractPrUrl(artifact)
  return (
    <div className="p-6">
      <div className="rounded-2xl border bg-card p-5 shadow-sm">
        <div className="text-muted-foreground text-xs uppercase tracking-wider">
          Pull request
        </div>
        <h3 className="mt-1 font-semibold text-base">
          {artifact.title ?? 'Pull request'}
        </h3>
        {artifact.description && (
          <p className="mt-2 text-muted-foreground text-sm">
            {artifact.description}
          </p>
        )}
        {url ? (
          <a
            className="mt-4 inline-flex items-center gap-1 font-medium text-primary text-sm hover:underline"
            href={url}
            rel="noreferrer"
            target="_blank"
          >
            Open on GitHub <ExternalLink className="h-3 w-3" />
          </a>
        ) : (
          <p className="mt-4 text-muted-foreground text-sm">
            No URL available.
          </p>
        )}
      </div>
    </div>
  )
}
