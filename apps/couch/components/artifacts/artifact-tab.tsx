'use client'

import { FileQuestion, FileText, GitPullRequest, X } from 'lucide-react'
import type { AhsArtifact } from '@/lib/ahs/server/client'
import { cn } from '@/lib/utils'

function iconFor(type: string) {
  if (type === 'TEXT') return FileText
  if (type === 'PR') return GitPullRequest
  return FileQuestion
}

export function ArtifactTab({
  artifact,
  active,
  onSelect,
  onClose,
}: {
  artifact: AhsArtifact
  active: boolean
  onSelect: () => void
  onClose: () => void
}) {
  const Icon = iconFor(artifact.artifact_type)
  return (
    <button
      className={cn(
        'flex max-w-[180px] cursor-pointer items-center gap-1.5 border-r px-3 py-1.5 text-sm',
        active ? 'bg-card font-medium' : 'bg-muted text-muted-foreground'
      )}
      onClick={onSelect}
      type="button"
    >
      <Icon className="h-3.5 w-3.5" />
      <span className="truncate">
        {artifact.title ?? artifact.id.slice(0, 8)}
      </span>
      <span
        aria-label="close tab"
        className="ml-1 rounded p-0.5 opacity-60 hover:bg-accent hover:opacity-100"
        onClick={(e) => {
          e.stopPropagation()
          onClose()
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.stopPropagation()
            onClose()
          }
        }}
        role="button"
        tabIndex={0}
      >
        <X className="h-3 w-3" />
      </span>
    </button>
  )
}
