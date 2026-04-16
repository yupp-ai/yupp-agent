import type { StreamItemAction } from '@yupp/agents-protocol/stream-items'
import { applyStreamItemActions } from '@yupp/agents-protocol/stream-items'
import type { StreamItem } from '@yupp/agents-ui/types'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AHS_HEARTBEAT_TIMEOUT_MS,
  AHS_RECONNECT_BASE_DELAY_MS,
  AHS_RECONNECT_MAX_ATTEMPTS,
  AHS_RECONNECT_MAX_DELAY_MS,
} from '@/lib/ahs/constants'
import {
  getMessageText,
  isOptimisticUserMessageItem,
  OPTIMISTIC_USER_ITEM_ID_PREFIX,
} from '@/lib/ahs/optimistic-user-message'
import {
  isSessionActive as isActiveSessionId,
  isCurrentSessionConnectionAttempt,
} from '@/lib/ahs/session-connection-guard'
import type { AhsClientMessage, AhsServerEvent } from '@/lib/ahs/types'
import { useSession } from '@/lib/auth/session-provider'
import { useTRPCClient } from '@/lib/hooks/trpc-client'

export type AhsConnectionStatus =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'error'

export interface UseAhsSessionOptions {
  sessionId: string | null
}

export interface UseAhsSessionReturn {
  connectionStatus: AhsConnectionStatus
  items: StreamItem[]
  turnStatus: 'idle' | 'running'
  sendMessage: (content: string) => void
  stop: () => void
  lastEventId: number
}

function getCanonicalUserMessageText(event: AhsServerEvent): string | null {
  if (event.type !== 'item/started' || event.item.type !== 'user_message') {
    return null
  }

  const text = 'text' in event.item ? event.item.text : undefined
  return text ?? ''
}

function removeMatchingOptimisticUserMessage(
  items: StreamItem[],
  canonicalText: string
): StreamItem[] {
  let removed = false
  return items.filter((item) => {
    if (removed || !isOptimisticUserMessageItem(item)) return true
    if (getMessageText(item) !== canonicalText) return true
    removed = true
    return false
  })
}

function isToolStreamItem(item: StreamItem): boolean {
  return (
    item.type === 'command_execution' ||
    item.type === 'file_change' ||
    item.type === 'mcp_tool_call'
  )
}

// TODO(ax): This is a defensive fix to complete the tool calls at the end of the turn. Once AHS is fixed, we can probably remove this
export function finalizeStreamingToolItemsForTurn(
  items: StreamItem[],
  options: {
    turnId?: string
    status: 'complete' | 'error'
  }
): StreamItem[] {
  const { turnId, status } = options
  if (!turnId) return items

  return items.map((item) => {
    if (!isToolStreamItem(item) || item.status !== 'streaming') {
      return item
    }
    if (item.turnId !== turnId) {
      return item
    }

    return {
      ...item,
      status,
    }
  })
}

interface AhsStreamTranslationState {
  itemIdMap: Map<string, string>
  counter: number
}

export function createAhsStreamTranslationState(): AhsStreamTranslationState {
  return {
    itemIdMap: new Map<string, string>(),
    counter: 0,
  }
}

function nextStreamItemId(state: AhsStreamTranslationState): string {
  state.counter += 1
  return `ahs-${state.counter}-${Date.now()}`
}

function resolveStreamItemId(
  state: AhsStreamTranslationState,
  ahsItemId: string
): string {
  const existing = state.itemIdMap.get(ahsItemId)
  if (existing) return existing

  const id = nextStreamItemId(state)
  state.itemIdMap.set(ahsItemId, id)
  return id
}

export function translateAhsServerEvent(
  event: AhsServerEvent,
  state: AhsStreamTranslationState
): StreamItemAction[] {
  const now = Date.now()

  switch (event.type) {
    case 'thread/started':
      return []

    case 'turn/started':
      return []

    case 'turn/completed':
      if (event.status === 'failed') {
        return [
          {
            type: 'create',
            item: {
              id: `turn-failed-${event.turn_id}`,
              type: 'error',
              status: 'error',
              timestamp: now,
              turnId: event.turn_id,
              data: {
                message: event.error?.message ?? 'Turn failed',
              },
            },
          },
        ]
      }
      return [
        {
          type: 'create',
          item: {
            id: `turn-completed-${event.turn_id}`,
            type: 'turn_complete',
            status: 'complete',
            timestamp: now,
            turnId: event.turn_id,
            data: {
              durationMs: event.usage?.duration_ms,
              costUsd: event.usage?.cost_usd,
            },
          },
        },
      ]

    case 'item/started': {
      const item = event.item
      const id = resolveStreamItemId(state, item.id)

      switch (item.type) {
        case 'user_message':
          return [
            {
              type: 'create',
              item: {
                id,
                itemId: item.id,
                type: 'message',
                status: 'complete',
                timestamp: now,
                threadId: event.thread_id,
                turnId: event.turn_id,
                data: {
                  role: 'user',
                  text: 'text' in item ? (item.text ?? '') : '',
                },
              },
            },
          ]

        case 'agent_message':
          return [
            {
              type: 'create',
              item: {
                id,
                itemId: item.id,
                type: 'message',
                status: 'streaming',
                timestamp: now,
                threadId: event.thread_id,
                turnId: event.turn_id,
                data: { role: 'assistant', text: '' },
              },
            },
          ]

        case 'command_execution':
          return [
            {
              type: 'create',
              item: {
                id,
                itemId: item.id,
                type: 'command_execution',
                status: 'streaming',
                timestamp: now,
                threadId: event.thread_id,
                turnId: event.turn_id,
                data: {
                  command: 'command' in item ? (item.command ?? '') : '',
                },
              },
            },
          ]

        case 'file_change':
          return [
            {
              type: 'create',
              item: {
                id,
                itemId: item.id,
                type: 'file_change',
                status: 'streaming',
                timestamp: now,
                threadId: event.thread_id,
                turnId: event.turn_id,
                data: {
                  changes: 'changes' in item ? (item.changes ?? []) : [],
                },
              },
            },
          ]

        case 'mcp_tool_call':
          return [
            {
              type: 'create',
              item: {
                id,
                itemId: item.id,
                type: 'mcp_tool_call',
                status: 'streaming',
                timestamp: now,
                threadId: event.thread_id,
                turnId: event.turn_id,
                data: {
                  server: 'server' in item ? (item.server ?? '') : '',
                  name: 'tool' in item ? (item.tool ?? '') : '',
                  arguments: 'arguments' in item ? (item.arguments ?? {}) : {},
                },
              },
            },
          ]

        default:
          return [
            {
              type: 'create',
              item: {
                id,
                type: 'status',
                status: 'complete',
                timestamp: now,
                data: {
                  message: `Unknown item type: ${item.type}`,
                },
              },
            },
          ]
      }
    }

    case 'item/completed': {
      const item = event.item
      const id = state.itemIdMap.get(item.id)
      if (!id) return []

      switch (item.type) {
        case 'user_message':
          return [
            {
              type: 'complete',
              id,
              patch: {
                data: {
                  role: 'user',
                  text: 'text' in item ? (item.text ?? '') : '',
                },
              },
            },
          ]

        case 'agent_message':
          return [
            {
              type: 'complete',
              id,
              patch: {
                data: {
                  role: 'assistant',
                  text: 'text' in item ? (item.text ?? '') : '',
                },
              },
            },
          ]

        case 'command_execution':
          return [
            {
              type: 'complete',
              id,
              patch: {
                data: {
                  command: 'command' in item ? (item.command ?? '') : '',
                  stdout:
                    'aggregated_output' in item
                      ? (item.aggregated_output ?? '')
                      : '',
                  exitCode: 'exit_code' in item ? item.exit_code : undefined,
                },
              },
            },
          ]

        case 'file_change':
          return [
            {
              type: 'complete',
              id,
              patch: {
                data: {
                  changes: 'changes' in item ? (item.changes ?? []) : [],
                },
              },
            },
          ]

        case 'mcp_tool_call': {
          const data: Record<string, unknown> = {
            server: 'server' in item ? (item.server ?? '') : '',
            name: 'tool' in item ? (item.tool ?? '') : '',
            arguments: 'arguments' in item ? (item.arguments ?? {}) : {},
          }
          if ('result' in item && item.result !== undefined) {
            data.result = item.result
          }
          if ('error' in item && item.error) {
            data.error = item.error.message
          }
          return [{ type: 'complete', id, patch: { data } }]
        }

        default:
          return [{ type: 'complete', id }]
      }
    }

    case 'item/agentMessage/delta': {
      const id = state.itemIdMap.get(event.item_id)
      if (!id) return []
      return [{ type: 'append_text', id, text: event.delta }]
    }

    case 'heartbeat':
      return []

    case 'error':
      return [
        {
          type: 'create',
          item: {
            id: nextStreamItemId(state),
            type: 'error',
            status: 'error',
            timestamp: now,
            data: { message: event.message },
          },
        },
      ]

    default:
      return []
  }
}

export function useAhsSession({
  sessionId,
}: UseAhsSessionOptions): UseAhsSessionReturn {
  const session = useSession()
  const [connectionStatus, setConnectionStatus] =
    useState<AhsConnectionStatus>('disconnected')
  const [items, setItems] = useState<StreamItem[]>([])
  const [turnStatus, setTurnStatus] = useState<'idle' | 'running'>('idle')
  const [lastEventId, setLastEventId] = useState(0)

  const trpcClient = useTRPCClient()

  const wsRef = useRef<WebSocket | null>(null)
  const translationStateRef = useRef(createAhsStreamTranslationState())
  const reconnectAttemptRef = useRef(0)
  const activeSessionIdRef = useRef<string | null>(sessionId)
  const connectionGenerationRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined
  )
  const heartbeatTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined
  )
  const eventCountRef = useRef(0)

  const isSessionActive = useCallback((sid: string): boolean => {
    return isActiveSessionId(activeSessionIdRef.current, sid)
  }, [])

  const isCurrentConnectionAttempt = useCallback(
    (sid: string, generation: number): boolean => {
      return isCurrentSessionConnectionAttempt({
        activeSessionId: activeSessionIdRef.current,
        sessionId: sid,
        currentGeneration: connectionGenerationRef.current,
        attemptGeneration: generation,
      })
    },
    []
  )

  const isCurrentSocketForSession = useCallback(
    (ws: WebSocket, sid: string) => {
      return wsRef.current === ws && isSessionActive(sid)
    },
    [isSessionActive]
  )

  const resetHeartbeatTimer = useCallback(() => {
    if (heartbeatTimerRef.current) clearTimeout(heartbeatTimerRef.current)
    heartbeatTimerRef.current = setTimeout(() => {
      // No message received within timeout — force close to trigger reconnect
      wsRef.current?.close()
    }, AHS_HEARTBEAT_TIMEOUT_MS)
  }, [])

  const cleanup = useCallback(() => {
    connectionGenerationRef.current += 1
    if (heartbeatTimerRef.current) clearTimeout(heartbeatTimerRef.current)
    if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
    heartbeatTimerRef.current = undefined
    reconnectTimerRef.current = undefined
    if (wsRef.current) {
      wsRef.current.onopen = null
      wsRef.current.onmessage = null
      wsRef.current.onerror = null
      wsRef.current.onclose = null
      wsRef.current.close()
      wsRef.current = null
    }
  }, [])

  const connect = useCallback(
    async (sid: string) => {
      if (!isSessionActive(sid)) return

      cleanup()
      if (!isSessionActive(sid)) return
      const generation = connectionGenerationRef.current

      setConnectionStatus(
        reconnectAttemptRef.current > 0 ? 'reconnecting' : 'connecting'
      )

      let wsUrl: string
      try {
        const result = await trpcClient.ahs.getWsUrl.query({
          sessionId: sid,
        })
        wsUrl = result.url
      } catch {
        if (isCurrentConnectionAttempt(sid, generation)) {
          setConnectionStatus('error')
        }
        return
      }

      if (!isCurrentConnectionAttempt(sid, generation)) return

      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        if (!isCurrentSocketForSession(ws, sid)) {
          ws.close()
          return
        }
        setConnectionStatus('connected')
        reconnectAttemptRef.current = 0
        resetHeartbeatTimer()
      }

      ws.onmessage = (e) => {
        if (!isCurrentSocketForSession(ws, sid)) return
        resetHeartbeatTimer()

        let event: AhsServerEvent
        try {
          event = JSON.parse(e.data as string)
        } catch {
          return
        }

        eventCountRef.current++
        setLastEventId(eventCountRef.current)

        // Track turn status
        if (event.type === 'turn/started') {
          setTurnStatus('running')
        } else if (event.type === 'turn/completed') {
          setTurnStatus('idle')
        }

        const actions = translateAhsServerEvent(
          event,
          translationStateRef.current
        )
        const canonicalUserMessageText = getCanonicalUserMessageText(event)

        if (actions.length > 0 || canonicalUserMessageText !== null) {
          setItems((prev) => {
            let next = prev
            if (canonicalUserMessageText !== null) {
              next = removeMatchingOptimisticUserMessage(
                next,
                canonicalUserMessageText
              )
            }
            if (actions.length > 0) {
              next = applyStreamItemActions(next, actions)
            }
            if (event.type === 'turn/completed') {
              next = finalizeStreamingToolItemsForTurn(next, {
                turnId: event.turn_id,
                status: event.status === 'failed' ? 'error' : 'complete',
              })
            }
            return next
          })
        }
      }

      ws.onerror = () => {
        // onclose will fire after onerror
      }

      ws.onclose = () => {
        if (wsRef.current !== ws) return
        if (heartbeatTimerRef.current) clearTimeout(heartbeatTimerRef.current)
        heartbeatTimerRef.current = undefined
        wsRef.current = null

        if (!isSessionActive(sid)) return

        if (reconnectAttemptRef.current >= AHS_RECONNECT_MAX_ATTEMPTS) {
          setConnectionStatus('error')
          return
        }

        setConnectionStatus('reconnecting')
        const exponentialDelay = Math.min(
          AHS_RECONNECT_BASE_DELAY_MS * 2 ** reconnectAttemptRef.current,
          AHS_RECONNECT_MAX_DELAY_MS
        )
        const jitter = Math.random() * AHS_RECONNECT_BASE_DELAY_MS
        const delay = Math.min(
          exponentialDelay + jitter,
          AHS_RECONNECT_MAX_DELAY_MS
        )
        reconnectAttemptRef.current++
        reconnectTimerRef.current = setTimeout(() => {
          if (!isSessionActive(sid)) return
          void connect(sid)
        }, delay)
      }
    },
    [
      cleanup,
      isCurrentConnectionAttempt,
      isCurrentSocketForSession,
      isSessionActive,
      resetHeartbeatTimer,
      trpcClient,
    ]
  )

  // Connect / disconnect based on sessionId
  useEffect(() => {
    activeSessionIdRef.current = sessionId

    if (!sessionId) {
      cleanup()
      setConnectionStatus('disconnected')
      setItems([])
      setTurnStatus('idle')
      setLastEventId(0)
      translationStateRef.current = createAhsStreamTranslationState()
      eventCountRef.current = 0
      reconnectAttemptRef.current = 0
      return
    }

    translationStateRef.current = createAhsStreamTranslationState()
    eventCountRef.current = 0
    reconnectAttemptRef.current = 0
    setItems([])
    setTurnStatus('idle')
    setLastEventId(0)
    connect(sessionId)

    return () => {
      activeSessionIdRef.current = null
      cleanup()
      setConnectionStatus('disconnected')
    }
  }, [sessionId, connect, cleanup])

  const sendWsMessage = useCallback((msg: AhsClientMessage): boolean => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      return false
    }

    try {
      wsRef.current.send(JSON.stringify(msg))
      return true
    } catch {
      return false
    }
  }, [])

  const resubscribeWs = useCallback(
    (sid: string) => {
      const readyState = wsRef.current?.readyState
      if (
        readyState === WebSocket.OPEN ||
        readyState === WebSocket.CONNECTING
      ) {
        return
      }

      // Treat manual send as a fresh reconnect attempt.
      reconnectAttemptRef.current = 0
      void connect(sid)
    },
    [connect]
  )

  const sendMessage = useCallback(
    (content: string) => {
      const trimmed = content.trim()
      if (!trimmed || !sessionId) return

      const optimisticId = `ahs-user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      const userItem: StreamItem = {
        id: optimisticId,
        itemId: `${OPTIMISTIC_USER_ITEM_ID_PREFIX}${optimisticId}`,
        type: 'message',
        status: 'complete',
        timestamp: Date.now(),
        data: { role: 'user', text: trimmed },
      }
      setItems((prev) => [...prev, userItem])

      const sentViaWs = sendWsMessage({
        type: 'user_message',
        session_id: sessionId,
        content: trimmed,
        user_id: session.user.id,
        source: 'websocket',
      })
      if (sentViaWs) return

      // Try to resubscribe so follow-up events can stream in without refocus.
      resubscribeWs(sessionId)

      // Fallback for dropped/closed websocket sessions: persist over REST.
      void trpcClient.ahs.sendMessage
        .mutate({
          message: trimmed,
          session_id: sessionId,
          source: 'api',
        })
        .catch(() => {
          setItems((prev) => prev.filter((item) => item.id !== optimisticId))
        })
    },
    [resubscribeWs, sendWsMessage, session.user.id, sessionId, trpcClient]
  )

  const stop = useCallback(() => {
    if (sessionId && !sendWsMessage({ type: 'stop', session_id: sessionId })) {
      void trpcClient.ahs.stopSession.mutate({ sessionId })
    }
  }, [sendWsMessage, sessionId, trpcClient])

  return {
    connectionStatus,
    items,
    turnStatus,
    sendMessage,
    stop,
    lastEventId,
  }
}
