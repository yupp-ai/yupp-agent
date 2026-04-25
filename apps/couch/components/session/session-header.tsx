'use client'

import { PanelRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { SessionMenu } from './session-menu'
import {
  type SessionStatus,
  StatusPill,
  type WsStatus,
} from './status-pill'

export function SessionHeader({
  sessionId,
  title,
  sessionStatus,
  wsStatus,
  turnInFlight,
  onTogglePanel,
  panelOpen,
}: {
  sessionId: string
  title: string
  sessionStatus: SessionStatus
  wsStatus: WsStatus
  turnInFlight: boolean
  onTogglePanel: () => void
  panelOpen: boolean
}) {
  return (
    <header className="flex h-12 items-center justify-between border-b bg-background px-4">
      <div className="flex items-center gap-2 text-sm">
        <span className="font-medium">Couch</span>
        <span className="text-muted-foreground">›</span>
        <span className="truncate font-medium">{title}</span>
        {turnInFlight && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-700 text-xs">
            Preview
          </span>
        )}
      </div>
      <div className="flex items-center gap-3">
        <StatusPill
          sessionStatus={sessionStatus}
          turnInFlight={turnInFlight}
          wsStatus={wsStatus}
        />
        <SessionMenu sessionId={sessionId} />
        <Button
          aria-label={panelOpen ? 'hide panel' : 'show panel'}
          onClick={onTogglePanel}
          size="icon"
          variant="ghost"
        >
          <PanelRight className="h-4 w-4" />
        </Button>
      </div>
    </header>
  )
}
