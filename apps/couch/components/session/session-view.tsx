'use client'

import { useState } from 'react'
import { FollowUpPrompt } from '@/components/prompt/follow-up-prompt'
import { useAhsSession } from '@/lib/hooks/use-ahs-session'
import { ChatFeed } from './chat-feed'
import { SessionHeader } from './session-header'
import type { SessionStatus } from './status-pill'

export function SessionView({
  sessionId,
  initialTitle,
  initialStatus,
}: {
  sessionId: string
  initialTitle: string
  initialStatus: SessionStatus
}) {
  const [panelOpen, setPanelOpen] = useState(false)
  const session = useAhsSession({ sessionId })
  const turnInFlight = session.turnStatus === 'running'

  return (
    <div className="flex h-screen flex-1 flex-col">
      <SessionHeader
        onTogglePanel={() => setPanelOpen((v) => !v)}
        panelOpen={panelOpen}
        sessionStatus={initialStatus}
        title={initialTitle}
        turnInFlight={turnInFlight}
        wsStatus={session.connectionStatus}
      />
      <div className="flex flex-1 overflow-hidden">
        <main className="flex flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto px-4 py-6">
            <div className="mx-auto max-w-3xl">
              <ChatFeed sessionId={sessionId} />
            </div>
          </div>
          <FollowUpPrompt sessionId={sessionId} />
        </main>
        {panelOpen && (
          <aside className="w-[480px] border-l bg-card">
            <div className="p-4 text-muted-foreground text-sm">
              Artifacts panel — wired in chunk C.
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
