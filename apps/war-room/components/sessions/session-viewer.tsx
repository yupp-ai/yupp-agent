'use client'

import { useQuery } from '@tanstack/react-query'
import { Feed, type FeedHandle } from '@yupp/agents-ui/feed'
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  type PromptInputMessage,
  PromptInputSubmit,
  PromptInputTextarea,
} from '@yupp/agents-ui/prompt-input'
import { Shimmer } from '@yupp/agents-ui/shimmer'
import type { StreamItem } from '@yupp/agents-ui/types'
import { useCallback, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/components/ui/utils'
import {
  mergeSessionFeedItems,
  SESSION_HISTORY_ITEM_ID_PREFIX,
} from '@/lib/ahs/session-feed'
import { statusColor } from '@/lib/ahs/session-status'
import {
  getSessionDisplayTitle,
  normalizeSessionTitle,
} from '@/lib/ahs/session-title'
import type { AhsMessageHistoryItem } from '@/lib/ahs/types'
import { useTRPC } from '@/lib/hooks/trpc-client'
import { useAhsSession } from '@/lib/hooks/use-ahs-session'
import { useAutofocusOnKeyPress } from '@/lib/hooks/use-autofocus-on-key-press'

export type SessionViewerLayout = 'page' | 'pane'

export interface SessionViewerProps {
  sessionId: string
  layout: SessionViewerLayout
}

function connectionDotClass(
  status: 'disconnected' | 'connecting' | 'connected' | 'reconnecting' | 'error'
): string {
  switch (status) {
    case 'connected':
      return 'bg-emerald-400'
    case 'connecting':
      return 'bg-amber-400'
    case 'reconnecting':
      return 'bg-orange-400'
    default:
      return 'bg-zinc-500'
  }
}

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

function getContainerClassName(layout: SessionViewerLayout): string {
  return cn(
    'relative flex h-full min-h-0 flex-col',
    layout === 'page'
      ? 'mx-auto max-w-3xl p-4'
      : 'overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950/60 shadow-2xl shadow-black/20'
  )
}

function SessionViewerLoading({ layout }: { layout: SessionViewerLayout }) {
  if (layout === 'page') {
    return (
      <div className="mx-auto max-w-3xl p-4">
        <div className="mb-6 flex items-center gap-3">
          <Skeleton className="h-2.5 w-2.5 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-5 w-40" />
            <Skeleton className="h-3 w-64" />
          </div>
          <Skeleton className="h-5 w-20 rounded-full" />
        </div>
        <div className="flex">
          <Skeleton className="ml-auto h-12 w-56 rounded-xl" />
        </div>
      </div>
    )
  }

  return (
    <div className={getContainerClassName(layout)}>
      <div
        className={cn(
          'flex items-center gap-3',
          'border-b border-zinc-800 px-4 py-4'
        )}
      >
        <Skeleton className="h-2.5 w-2.5 rounded-full" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-3 w-64" />
        </div>
        <Skeleton className="h-5 w-20 rounded-full" />
      </div>

      <div className={cn('flex-1 space-y-3', 'px-4 py-4')}>
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-4/5" />
        <Skeleton className="h-4 w-3/5" />
      </div>
    </div>
  )
}

function SessionViewerError({
  layout,
  message,
}: {
  layout: SessionViewerLayout
  message: string
}) {
  if (layout === 'page') {
    return (
      <div className="mx-auto max-w-3xl p-4">
        <p className="text-sm text-red-400">{message}</p>
      </div>
    )
  }

  return (
    <div className={getContainerClassName(layout)}>
      <div className={cn('px-4 py-4')}>
        <p className="text-sm text-red-400">{message}</p>
      </div>
    </div>
  )
}

export function SessionViewer({ sessionId, layout }: SessionViewerProps) {
  const trpc = useTRPC()
  const feedHandleRef = useRef<FeedHandle | null>(null)
  const promptTextareaRef = useRef<HTMLTextAreaElement | null>(null)
  const [canJumpToLiveEdge, setCanJumpToLiveEdge] = useState(false)

  const handlePromptMount = useCallback((node: HTMLDivElement | null) => {
    promptTextareaRef.current = node?.querySelector('textarea') ?? null
  }, [])

  const {
    data: sessionDetail,
    isLoading: sessionLoading,
    error: sessionError,
  } = useQuery(trpc.ahs.getSession.queryOptions({ id: sessionId }))

  const session = sessionDetail?.session ?? null
  const sessionTitle = session ? normalizeSessionTitle(session.title) : null
  const showSessionId = layout === 'pane' || Boolean(sessionTitle)
  const hasSessionMetadata = Boolean(session?.agent_name || showSessionId)
  const isActive = session?.status === 'ACTIVE'
  // Follow-ups are intentionally allowed for COMPLETED/STALE sessions.
  // AHS can accept a new user message and start a new turn on the same session.
  const canFollowUp = session != null

  const {
    connectionStatus,
    items: liveItems,
    sendMessage,
  } = useAhsSession({ sessionId })

  const {
    data: historyData,
    isLoading: historyLoading,
    error: historyError,
  } = useQuery({
    ...trpc.ahs.getSessionHistory.queryOptions({ id: sessionId }),
    refetchInterval: connectionStatus === 'connected' ? false : 1_500,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: false,
    refetchOnReconnect: true,
  })

  const historyItems = useMemo(
    () => historyToStreamItems(sessionId, historyData?.messages ?? []),
    [historyData, sessionId]
  )

  const allItems = useMemo(
    () =>
      mergeSessionFeedItems(historyItems, liveItems, {
        assistantTurnPreference:
          connectionStatus === 'connected' ? 'live' : 'history',
      }),
    [connectionStatus, historyItems, liveItems]
  )

  const loading = sessionLoading || historyLoading
  const error = sessionError ?? historyError

  const handleSubmit = useCallback(
    ({ text }: PromptInputMessage) => {
      const content = text.trim()
      if (content) {
        sendMessage(content)
      }
    },
    [sendMessage]
  )

  const handleScrollToBottom = useCallback(() => {
    setCanJumpToLiveEdge(false)
    feedHandleRef.current?.requestLiveEdge()
  }, [])

  useAutofocusOnKeyPress(promptTextareaRef, canFollowUp && layout === 'page')

  if (loading) {
    return <SessionViewerLoading layout={layout} />
  }

  if (error) {
    return <SessionViewerError layout={layout} message={error.message} />
  }

  if (layout === 'page') {
    return (
      <>
        <div className={`mx-auto max-w-3xl p-4 ${canFollowUp ? 'pb-44' : ''}`}>
          <div className="mb-4 flex items-center gap-3">
            <span
              className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                session ? statusColor(session.status) : 'bg-zinc-500'
              }`}
            />
            <div className="min-w-0 flex-1">
              <h1 className="truncate font-bold text-lg tracking-tight">
                {session ? getSessionDisplayTitle(session) : 'Session'}
              </h1>
              {hasSessionMetadata && (
                <p className="flex flex-wrap gap-x-2 gap-y-1 text-xs text-zinc-500">
                  {session?.agent_name && <span>{session.agent_name}</span>}
                  {sessionTitle && (
                    <span className="font-mono text-zinc-400">{sessionId}</span>
                  )}
                </p>
              )}
            </div>
            {session && <Badge>{session.status}</Badge>}
            {isActive && (
              <span
                className={`h-2 w-2 rounded-full ${connectionDotClass(connectionStatus)}`}
                title={connectionStatus}
              />
            )}
          </div>

          {allItems.length > 0 ? (
            <Feed
              handleRef={feedHandleRef}
              items={allItems}
              onCanJumpToLiveEdgeChange={setCanJumpToLiveEdge}
            />
          ) : (
            <Shimmer className="text-sm" duration={2}>
              {isActive ? 'Waiting for events' : 'No messages in this session'}
            </Shimmer>
          )}
        </div>

        {canJumpToLiveEdge && (
          <div
            className={`pointer-events-none fixed inset-x-0 z-50 flex justify-center px-4 ${
              canFollowUp
                ? 'bottom-[calc(env(safe-area-inset-bottom)+9.5rem)]'
                : 'bottom-[calc(env(safe-area-inset-bottom)+1.5rem)]'
            }`}
          >
            <Button
              aria-label="Scroll to latest message"
              className="cursor-pointer pointer-events-auto size-9 rounded-full border-zinc-700/80 bg-zinc-900/95 p-0 shadow-lg backdrop-blur-md hover:bg-zinc-800"
              onClick={handleScrollToBottom}
              type="button"
            >
              <svg
                aria-hidden="true"
                className="size-4"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2"
                viewBox="0 0 24 24"
              >
                <path d="M12 5v14" />
                <path d="m19 12-7 7-7-7" />
              </svg>
            </Button>
          </div>
        )}

        {canFollowUp && (
          <div className="fixed inset-x-0 bottom-0 z-40 mx-4 rounded-t-3xl bg-linear-to-t from-zinc-950 to-transparent backdrop-blur-md md:mx-auto md:max-w-3xl">
            <div
              className="mx-auto flex items-end gap-2 pb-[calc(env(safe-area-inset-bottom)+1rem)]"
              ref={handlePromptMount}
            >
              <PromptInput className="min-w-0 flex-1" onSubmit={handleSubmit}>
                <PromptInputBody>
                  <PromptInputTextarea
                    autoComplete="off"
                    placeholder={
                      session?.agent_name
                        ? `Ask ${session.agent_name} anything`
                        : 'Ask anything'
                    }
                  />
                </PromptInputBody>
                <PromptInputFooter className="justify-end">
                  <PromptInputSubmit />
                </PromptInputFooter>
              </PromptInput>
            </div>
          </div>
        )}
      </>
    )
  }

  return (
    <section className={getContainerClassName(layout)}>
      <div
        className={cn(
          'flex items-center gap-3',
          'border-b border-zinc-800 px-4 py-4'
        )}
      >
        <span
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${
            session ? statusColor(session.status) : 'bg-zinc-500'
          }`}
        />
        <div className="min-w-0 flex-1">
          <h1 className="truncate font-bold text-lg tracking-tight">
            {session ? getSessionDisplayTitle(session) : 'Session'}
          </h1>
          {hasSessionMetadata && (
            <p className="flex flex-wrap gap-x-2 gap-y-1 text-xs text-zinc-500">
              {session?.agent_name && <span>{session.agent_name}</span>}
              {showSessionId && (
                <span className="font-mono text-zinc-400">{sessionId}</span>
              )}
            </p>
          )}
        </div>
        {session && <Badge>{session.status}</Badge>}
        {isActive && (
          <span
            className={`h-2 w-2 rounded-full ${connectionDotClass(connectionStatus)}`}
            title={connectionStatus}
          />
        )}
      </div>

      <div className={cn('min-h-0 flex-1 overflow-y-auto', 'px-4 py-4')}>
        {allItems.length > 0 ? (
          <Feed
            handleRef={feedHandleRef}
            items={allItems}
            onCanJumpToLiveEdgeChange={setCanJumpToLiveEdge}
          />
        ) : (
          <Shimmer className="text-sm" duration={2}>
            {isActive ? 'Waiting for events' : 'No messages in this session'}
          </Shimmer>
        )}
      </div>

      {canJumpToLiveEdge && (
        <div
          className={cn(
            'pointer-events-none absolute inset-x-0 z-10 flex justify-center px-4',
            canFollowUp ? 'bottom-28' : 'bottom-4'
          )}
        >
          <Button
            aria-label="Scroll to latest message"
            className="cursor-pointer pointer-events-auto size-9 rounded-full border-zinc-700/80 bg-zinc-900/95 p-0 shadow-lg backdrop-blur-md hover:bg-zinc-800"
            onClick={handleScrollToBottom}
            type="button"
          >
            <svg
              aria-hidden="true"
              className="size-4"
              fill="none"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="2"
              viewBox="0 0 24 24"
            >
              <path d="M12 5v14" />
              <path d="m19 12-7 7-7-7" />
            </svg>
          </Button>
        </div>
      )}

      {canFollowUp && (
        <div
          className={cn(
            'border-zinc-800 bg-linear-to-t from-zinc-950 via-zinc-950/98 to-zinc-950/90 backdrop-blur-md',
            'border-t'
          )}
        >
          <div
            className="flex items-end gap-2 p-4 pb-[calc(env(safe-area-inset-bottom)+1rem)]"
            ref={handlePromptMount}
          >
            <PromptInput className="min-w-0 flex-1" onSubmit={handleSubmit}>
              <PromptInputBody>
                <PromptInputTextarea
                  autoComplete="off"
                  placeholder={
                    session?.agent_name
                      ? `Ask ${session.agent_name} anything`
                      : 'Ask anything'
                  }
                />
              </PromptInputBody>
              <PromptInputFooter className="justify-end">
                <PromptInputSubmit />
              </PromptInputFooter>
            </PromptInput>
          </div>
        </div>
      )}
    </section>
  )
}
