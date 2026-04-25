'use client'

import { useQuery } from '@tanstack/react-query'
import { Feed } from '@yupp/agents-ui/feed'
import type { StreamItem } from '@yupp/agents-ui/types'
import { useMemo } from 'react'
import { getSessionHistoryAction } from '@/app/_actions/get-session-history'
import {
  mergeSessionFeedItems,
  SESSION_HISTORY_ITEM_ID_PREFIX,
} from '@/lib/ahs/session-feed'
import type { AhsMessageHistoryItem } from '@/lib/ahs/types'
import { useAhsSession } from '@/lib/hooks/use-ahs-session'
import { useSessionMenu } from '@/lib/hooks/use-session-menu'

function historyToStreamItems(
  sessionId: string,
  messages: AhsMessageHistoryItem[]
): StreamItem[] {
  return messages.map((message, index) => ({
    id: `${SESSION_HISTORY_ITEM_ID_PREFIX}${message.message_id}`,
    itemId: message.message_id,
    type: 'message' as const,
    status: 'complete' as const,
    timestamp: message.created_at
      ? new Date(message.created_at).getTime()
      : index,
    turnId: `${sessionId}:${message.turn_number}`,
    data: { role: message.role, text: message.content ?? '' },
  }))
}

const TOOL_ITEM_TYPES = new Set(['command_execution', 'mcp_tool_call'])

export function ChatFeed({ sessionId }: { sessionId: string }) {
  const historyQuery = useQuery({
    queryKey: ['session-history', sessionId],
    queryFn: () => getSessionHistoryAction(sessionId, { limit: 200 }),
  })
  const session = useAhsSession({ sessionId })
  const [{ toolCallsVisible }] = useSessionMenu(sessionId)

  const items = useMemo(() => {
    const history = historyToStreamItems(
      sessionId,
      historyQuery.data?.messages ?? []
    )
    const merged = mergeSessionFeedItems(history, session.items, {
      assistantTurnPreference: 'live',
    })
    if (toolCallsVisible) return merged
    return merged.filter((i) => !TOOL_ITEM_TYPES.has(i.type))
  }, [sessionId, historyQuery.data?.messages, session.items, toolCallsVisible])

  return (
    <div className="couch-feed">
      <Feed items={items} />
    </div>
  )
}
