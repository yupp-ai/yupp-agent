'use client'

import { Download, Maximize2, Plus } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { AhsArtifact } from '@/lib/ahs/server/client'
import { ArtifactPrView } from './artifact-pr-view'
import { ArtifactTab } from './artifact-tab'
import { ArtifactTextView } from './artifact-text-view'

export function ArtifactPanel({
  artifacts,
  onClose,
}: {
  artifacts: AhsArtifact[]
  onClose: () => void
}) {
  const sorted = useMemo(
    () =>
      [...artifacts].sort(
        (a, b) =>
          new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
      ),
    [artifacts]
  )
  const [closed, setClosed] = useState<Set<string>>(new Set())
  const open = sorted.filter((a) => !closed.has(a.id))
  const [activeId, setActiveId] = useState<string | null>(null)

  useEffect(() => {
    if (activeId && open.some((a) => a.id === activeId)) return
    if (open.length > 0) setActiveId(open[open.length - 1].id)
    else setActiveId(null)
  }, [open, activeId])

  const active = open.find((a) => a.id === activeId) ?? null

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center border-b">
        <div className="flex flex-1 overflow-x-auto">
          {open.map((a) => (
            <ArtifactTab
              active={a.id === activeId}
              artifact={a}
              key={a.id}
              onClose={() => setClosed((s) => new Set(s).add(a.id))}
              onSelect={() => setActiveId(a.id)}
            />
          ))}
          {closed.size > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger
                aria-label="reopen tab"
                className="flex items-center px-3 py-1.5 text-muted-foreground hover:text-foreground"
              >
                <Plus className="h-4 w-4" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                {sorted
                  .filter((a) => closed.has(a.id))
                  .map((a) => (
                    <DropdownMenuItem
                      key={a.id}
                      onSelect={() => {
                        setClosed((s) => {
                          const n = new Set(s)
                          n.delete(a.id)
                          return n
                        })
                        setActiveId(a.id)
                      }}
                    >
                      {a.title ?? a.id.slice(0, 8)}
                    </DropdownMenuItem>
                  ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
        <div className="flex items-center gap-1 border-l px-2">
          {active && (
            <a
              aria-label="download"
              className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
              download={active.title ?? active.id}
              href={`/api/ahs/artifact/${active.id}/content`}
            >
              <Download className="h-4 w-4" />
            </a>
          )}
          <Button
            aria-label="hide panel"
            onClick={onClose}
            size="icon"
            variant="ghost"
          >
            <Maximize2 className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        {!active && (
          <p className="p-4 text-muted-foreground text-sm">
            No artifact selected.
          </p>
        )}
        {active?.artifact_type === 'TEXT' && (
          <ArtifactTextView artifactId={active.id} />
        )}
        {active?.artifact_type === 'PR' && <ArtifactPrView artifact={active} />}
        {active &&
          active.artifact_type !== 'TEXT' &&
          active.artifact_type !== 'PR' && (
            <div className="p-4 text-sm">
              <p className="text-muted-foreground">
                Artifact type <code>{active.artifact_type}</code> isn't
                rendered inline yet.
              </p>
              <a
                className="mt-2 inline-block text-primary hover:underline"
                download={active.title ?? active.id}
                href={`/api/ahs/artifact/${active.id}/content`}
              >
                Download raw content →
              </a>
            </div>
          )}
      </div>
    </div>
  )
}
