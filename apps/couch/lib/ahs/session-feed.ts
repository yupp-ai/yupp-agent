import type { StreamItem } from '@yupp/agents-ui/types'
import {
  getMessageText,
  isOptimisticUserMessageItem,
} from './optimistic-user-message'

export const SESSION_HISTORY_ITEM_ID_PREFIX = 'history-message:'
const OPTIMISTIC_USER_RECONCILIATION_LOOKBACK_MS = 10_000
const LIVE_ASSISTANT_RECONCILIATION_LOOKBACK_MS = 10_000

interface MergeSessionFeedOptions {
  assistantTurnPreference?: 'history' | 'live'
}

/**
 * `id` is local stream-item identity in the feed.
 * `itemId` (when present) is the canonical backend item identity.
 *
 * History and websocket streams can overlap, so merge by stable identity:
 * prefer `itemId`, fall back to `id` for local-only items.
 */
function resolveStableMessageId(item: StreamItem): string {
  if (typeof item.itemId === 'string' && item.itemId.length > 0) {
    return item.itemId
  }
  return item.id
}

function isHistoryMessageItem(item: StreamItem): boolean {
  return (
    item.type === 'message' &&
    item.id.startsWith(SESSION_HISTORY_ITEM_ID_PREFIX)
  )
}

function getMessageData(item: StreamItem): Record<string, unknown> | null {
  if (item.type !== 'message' || !item.data) {
    return null
  }

  return item.data
}

function isCompleteAssistantMessageItem(item: StreamItem): boolean {
  const data = getMessageData(item)
  return (
    data !== null &&
    item.status === 'complete' &&
    data.role === 'assistant' &&
    typeof getMessageText(item) === 'string'
  )
}

function isCanonicalUserMessageItem(item: StreamItem): boolean {
  const data = getMessageData(item)
  return (
    data !== null &&
    data.role === 'user' &&
    !isOptimisticUserMessageItem(item) &&
    typeof getMessageText(item) === 'string'
  )
}

function isLiveAssistantMessageItem(item: StreamItem): boolean {
  return isCompleteAssistantMessageItem(item) && !isHistoryMessageItem(item)
}

function resolveStableTurnKey(turnId?: string): string | null {
  if (!turnId) return null
  const lastColonIndex = turnId.lastIndexOf(':')
  return lastColonIndex === -1 ? turnId : turnId.slice(lastColonIndex + 1)
}

/**
 * Optimistic user messages are kept for responsiveness, then removed once the
 * canonical user message arrives from history/ws with matching text + timing.
 */
function dropReconciledOptimisticUserMessages(
  items: StreamItem[]
): StreamItem[] {
  const canonicalUserTimestampsByText = new Map<string, number[]>()

  for (const item of items) {
    if (!isCanonicalUserMessageItem(item)) continue

    const text = getMessageText(item)
    if (!text) continue

    const timestamps = canonicalUserTimestampsByText.get(text) ?? []
    timestamps.push(item.timestamp)
    canonicalUserTimestampsByText.set(text, timestamps)
  }

  return items.filter((item) => {
    if (!isOptimisticUserMessageItem(item)) return true

    const text = getMessageText(item)
    if (!text) return true

    const candidateTimestamps = canonicalUserTimestampsByText.get(text)
    if (!candidateTimestamps || candidateTimestamps.length === 0) return true

    const earliestAcceptableTimestamp =
      item.timestamp - OPTIMISTIC_USER_RECONCILIATION_LOOKBACK_MS
    const matchIndex = candidateTimestamps.findIndex(
      (timestamp) => timestamp >= earliestAcceptableTimestamp
    )

    if (matchIndex === -1) return true

    candidateTimestamps.splice(matchIndex, 1)
    return false
  })
}

/**
 * Persisted history and websocket events can describe the same assistant
 * message with different identifiers. When turn IDs are missing, fall back to
 * matching by text/timing so the canonical history copy can replace the live
 * copy after long streams complete.
 */
function dropReconciledLiveAssistantMessages(
  items: StreamItem[]
): StreamItem[] {
  const canonicalAssistantEntriesByText = new Map<
    string,
    Array<{ timestamp: number; turnKey: string | null }>
  >()
  const canonicalUserMessageTimestamps: number[] = []

  for (const item of items) {
    if (isCanonicalUserMessageItem(item)) {
      canonicalUserMessageTimestamps.push(item.timestamp)
    }

    if (!isHistoryMessageItem(item) || !isCompleteAssistantMessageItem(item)) {
      continue
    }

    const text = getMessageText(item)
    if (!text) continue

    const entries = canonicalAssistantEntriesByText.get(text) ?? []
    entries.push({
      timestamp: item.timestamp,
      turnKey: resolveStableTurnKey(item.turnId),
    })
    canonicalAssistantEntriesByText.set(text, entries)
  }

  return items.filter((item) => {
    if (isHistoryMessageItem(item) || !isCompleteAssistantMessageItem(item)) {
      return true
    }

    const text = getMessageText(item)
    if (!text) return true

    const candidateEntries = canonicalAssistantEntriesByText.get(text)
    if (!candidateEntries || candidateEntries.length === 0) return true

    const liveTurnKey = resolveStableTurnKey(item.turnId)
    const matchIndex = candidateEntries.findIndex(({ timestamp, turnKey }) => {
      // When both sides have stable turn IDs, defer to the explicit
      // turn-preference reconciliation below instead of guessing by text.
      if (liveTurnKey && turnKey) {
        return false
      }

      const isWithinLookbackWindow =
        Math.abs(timestamp - item.timestamp) <=
        LIVE_ASSISTANT_RECONCILIATION_LOOKBACK_MS
      const isHistoryTimestampAfterLiveTimestamp = timestamp >= item.timestamp

      // History timestamps can reflect when the completed message was
      // persisted, while live items keep the stream start timestamp. Allow the
      // canonical history copy to replace the live copy even after long streams
      // as long as no canonical user message happened between them.
      if (!isWithinLookbackWindow && !isHistoryTimestampAfterLiveTimestamp) {
        return false
      }

      const start = Math.min(timestamp, item.timestamp)
      const end = Math.max(timestamp, item.timestamp)
      return !canonicalUserMessageTimestamps.some(
        (userTimestamp) => userTimestamp > start && userTimestamp < end
      )
    })

    if (matchIndex === -1) return true

    candidateEntries.splice(matchIndex, 1)
    return false
  })
}

/**
 * History and websocket streams can overlap on the same turn. Choose one
 * assistant source per turn so reconnect fallback polling does not duplicate
 * streamed replies.
 */
function dropOverlappedAssistantMessages(
  items: StreamItem[],
  options: Required<MergeSessionFeedOptions>
): StreamItem[] {
  const { assistantTurnPreference } = options
  const liveAssistantTurnIds = new Set<string>()
  const historyAssistantTurnIds = new Set<string>()

  for (const item of items) {
    if (isHistoryMessageItem(item)) {
      const turnKey = resolveStableTurnKey(item.turnId)
      if (!turnKey || !isCompleteAssistantMessageItem(item)) continue
      historyAssistantTurnIds.add(turnKey)
      continue
    }

    if (!isLiveAssistantMessageItem(item)) continue
    const turnKey = resolveStableTurnKey(item.turnId)
    if (!turnKey) continue
    liveAssistantTurnIds.add(turnKey)
  }

  return items.filter((item) => {
    if (!isCompleteAssistantMessageItem(item)) {
      return true
    }

    const turnKey = resolveStableTurnKey(item.turnId)
    if (!turnKey) return true

    if (assistantTurnPreference === 'history') {
      return isHistoryMessageItem(item) || !historyAssistantTurnIds.has(turnKey)
    }

    return !isHistoryMessageItem(item) || !liveAssistantTurnIds.has(turnKey)
  })
}

export function mergeSessionFeedItems(
  historyItems: StreamItem[],
  liveItems: StreamItem[],
  options: MergeSessionFeedOptions = {}
): StreamItem[] {
  const mergeOptions: Required<MergeSessionFeedOptions> = {
    assistantTurnPreference: options.assistantTurnPreference ?? 'live',
  }
  const combined = [...historyItems, ...liveItems].sort(
    (a, b) => a.timestamp - b.timestamp
  )
  const optimisticUsersReconciled =
    dropReconciledOptimisticUserMessages(combined)
  const liveAssistantsReconciled = dropReconciledLiveAssistantMessages(
    optimisticUsersReconciled
  )
  const reconciled = dropOverlappedAssistantMessages(
    liveAssistantsReconciled,
    mergeOptions
  )

  const seenMessageIds = new Set<string>()
  return reconciled.filter((item) => {
    if (item.type !== 'message') return true
    const stableMessageId = resolveStableMessageId(item)
    if (seenMessageIds.has(stableMessageId)) return false
    seenMessageIds.add(stableMessageId)
    return true
  })
}
