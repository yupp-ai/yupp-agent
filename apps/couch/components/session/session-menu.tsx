'use client'

import {
  Activity,
  Archive,
  BarChart3,
  Calendar,
  Copy,
  Eye,
  EyeOff,
  MoreHorizontal,
  Pencil,
  Pin,
  Square,
} from 'lucide-react'
import { useState, useTransition } from 'react'
import { stopSessionAction } from '@/app/_actions/stop-session'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useSessionMenu } from '@/lib/hooks/use-session-menu'

export function SessionMenu({ sessionId }: { sessionId: string }) {
  const [state, update] = useSessionMenu(sessionId)
  const [confirming, setConfirming] = useState(false)
  const [pending, startTransition] = useTransition()

  function handleStop() {
    startTransition(async () => {
      await stopSessionAction(sessionId)
      setConfirming(false)
    })
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="session menu"
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <MoreHorizontal className="h-4 w-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuItem
            onSelect={() => navigator.clipboard.writeText(sessionId)}
          >
            <Copy className="mr-2 h-4 w-4" /> Copy session ID
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setConfirming(true)}>
            <Square className="mr-2 h-4 w-4" /> Stop session
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => update({ toolCallsVisible: !state.toolCallsVisible })}
          >
            {state.toolCallsVisible ? (
              <EyeOff className="mr-2 h-4 w-4" />
            ) : (
              <Eye className="mr-2 h-4 w-4" />
            )}
            {state.toolCallsVisible ? 'Hide tool calls' : 'Show tool calls'}
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => {
              const next = window.prompt('Rename session', state.customTitle ?? '')
              if (next !== null) update({ customTitle: next || undefined })
            }}
          >
            <Pencil className="mr-2 h-4 w-4" /> Rename
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => update({ pinned: !state.pinned })}>
            <Pin className="mr-2 h-4 w-4" />
            {state.pinned ? 'Unpin' : 'Pin'}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem disabled>
            <Calendar className="mr-2 h-4 w-4" /> Schedule
          </DropdownMenuItem>
          <DropdownMenuItem disabled>
            <BarChart3 className="mr-2 h-4 w-4" /> Analyze
          </DropdownMenuItem>
          <DropdownMenuItem disabled>
            <Activity className="mr-2 h-4 w-4" /> Insights
          </DropdownMenuItem>
          <DropdownMenuItem disabled>
            <Archive className="mr-2 h-4 w-4" /> Archive
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {confirming && (
        <button
          aria-label="dismiss confirmation"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          onClick={() => setConfirming(false)}
          type="button"
        >
          <div
            className="w-full max-w-sm rounded-2xl border bg-card p-6 shadow-lg"
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
            role="dialog"
          >
            <h2 className="font-semibold text-base">Stop session?</h2>
            <p className="mt-2 text-muted-foreground text-sm">
              The current turn will be cancelled. The session can't be resumed
              after stopping.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                onClick={() => setConfirming(false)}
                variant="outline"
              >
                Cancel
              </Button>
              <Button
                disabled={pending}
                onClick={handleStop}
                variant="destructive"
              >
                {pending ? 'Stopping…' : 'Stop'}
              </Button>
            </div>
          </div>
        </button>
      )}
    </>
  )
}
