import { describe, expect, it } from 'bun:test'
import type { StreamItem } from '@yupp/agents-ui/types'
import { OPTIMISTIC_USER_ITEM_ID_PREFIX } from './optimistic-user-message'
import {
  mergeSessionFeedItems,
  SESSION_HISTORY_ITEM_ID_PREFIX,
} from './session-feed'

function messageItem({
  id,
  itemId,
  role = 'assistant',
  text,
  timestamp,
  turnId,
}: {
  id: string
  itemId?: string
  role?: 'user' | 'assistant'
  text: string
  timestamp: number
  turnId?: string
}): StreamItem {
  return {
    id,
    itemId,
    type: 'message',
    status: 'complete',
    timestamp,
    turnId,
    data: { role, text },
  }
}

function statusItem(id: string, timestamp: number): StreamItem {
  return {
    id,
    type: 'status',
    status: 'complete',
    timestamp,
    data: { message: 'status' },
  }
}

function optimisticUserItem({
  id,
  text,
  timestamp,
}: {
  id: string
  text: string
  timestamp: number
}): StreamItem {
  return {
    id,
    itemId: `${OPTIMISTIC_USER_ITEM_ID_PREFIX}${id}`,
    type: 'message',
    status: 'complete',
    timestamp,
    data: { role: 'user', text },
  }
}

describe('mergeSessionFeedItems', () => {
  it('prefers the live assistant copy over bootstrap history for the same turn', () => {
    const history = [
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
        itemId: 'msg-history-1',
        text: 'ok',
        timestamp: 1_000,
        turnId: 'rest-session:1',
      }),
    ]
    const live = [
      messageItem({
        id: 'live-1',
        itemId: 'msg-live-1',
        text: 'ok',
        timestamp: 1_500,
        turnId: 'ws-thread:1',
      }),
    ]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe('live-1')
  })

  it('deduplicates overlapping history/live messages by itemId', () => {
    const history = [
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-1`,
        itemId: 'msg-1',
        text: 'hello',
        timestamp: 1,
      }),
    ]
    const live = [
      messageItem({
        id: 'live-1',
        itemId: 'msg-1',
        text: 'hello',
        timestamp: 3,
      }),
    ]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe(`${SESSION_HISTORY_ITEM_ID_PREFIX}msg-1`)
  })

  it('falls back to stream item id when itemId is missing', () => {
    const history = [
      messageItem({ id: 'same-id', text: 'hello', timestamp: 1 }),
    ]
    const live = [messageItem({ id: 'same-id', text: 'hello', timestamp: 2 })]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe('same-id')
  })

  it('does not deduplicate non-message items', () => {
    const history = [statusItem('status-1', 1)]
    const live = [statusItem('status-1', 2)]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(2)
    expect(merged.map((item) => item.timestamp)).toEqual([1, 2])
  })

  it('keeps repeated assistant messages when they are far apart in time', () => {
    const history = [
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
        itemId: 'msg-history-1',
        text: 'ok',
        timestamp: 1_000,
      }),
    ]
    const live = [
      messageItem({
        id: 'live-1',
        itemId: 'msg-live-1',
        text: 'ok',
        timestamp: 12_500,
      }),
    ]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(2)
    expect(merged.map((item) => item.id)).toEqual([
      `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
      'live-1',
    ])
  })

  it('prefers the history assistant copy after a long stream completes when turn IDs are unavailable', () => {
    const history = [
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
        itemId: 'msg-history-1',
        text: 'ok',
        timestamp: 15_000,
      }),
    ]
    const live = [
      messageItem({
        id: 'live-1',
        itemId: 'msg-live-1',
        text: 'ok',
        timestamp: 1_000,
      }),
    ]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe(`${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`)
  })

  it('keeps bootstrap history assistant when live assistant is from a different turn', () => {
    const history = [
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
        itemId: 'msg-history-1',
        text: 'ok',
        timestamp: 15_000,
        turnId: 'thread-1:1',
      }),
    ]
    const live = [
      messageItem({
        id: 'live-1',
        itemId: 'msg-live-1',
        text: 'ok',
        timestamp: 1_000,
        turnId: 'thread-1:2',
      }),
    ]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(2)
    expect(merged.map((item) => item.id)).toEqual([
      'live-1',
      `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
    ])
  })

  it('prefers the history assistant copy when websocket fallback polling is active', () => {
    const history = [
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
        itemId: 'msg-history-1',
        text: 'final',
        timestamp: 2_000,
        turnId: 'rest-session:7',
      }),
    ]
    const live = [
      messageItem({
        id: 'live-1',
        itemId: 'msg-live-1',
        text: 'partial',
        timestamp: 1_000,
        turnId: 'ws-thread:7',
      }),
    ]

    const merged = mergeSessionFeedItems(history, live, {
      assistantTurnPreference: 'history',
    })

    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe(`${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`)
  })

  it('keeps repeated assistant messages from separate turns even when they are close in time', () => {
    const history = [
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
        itemId: 'msg-history-1',
        text: 'ok',
        timestamp: 1_000,
        turnId: 'thread-1:1',
      }),
      messageItem({
        id: `${SESSION_HISTORY_ITEM_ID_PREFIX}user-history-1`,
        itemId: 'user-history-1',
        role: 'user',
        text: 'again',
        timestamp: 1_200,
        turnId: 'thread-1:2',
      }),
    ]
    const live = [
      messageItem({
        id: 'live-1',
        itemId: 'msg-live-1',
        text: 'ok',
        timestamp: 1_500,
        turnId: 'thread-1:2',
      }),
    ]

    const merged = mergeSessionFeedItems(history, live)

    expect(merged).toHaveLength(3)
    expect(merged.map((item) => item.id)).toEqual([
      `${SESSION_HISTORY_ITEM_ID_PREFIX}msg-history-1`,
      `${SESSION_HISTORY_ITEM_ID_PREFIX}user-history-1`,
      'live-1',
    ])
  })

  it('removes optimistic user message when canonical message appears', () => {
    const optimistic = [
      optimisticUserItem({
        id: 'optimistic-1',
        text: 'hello',
        timestamp: 1000,
      }),
    ]
    const canonical = [
      messageItem({
        id: 'canonical-1',
        itemId: 'ahs-user-1',
        role: 'user',
        text: 'hello',
        timestamp: 1002,
      }),
    ]

    const merged = mergeSessionFeedItems(canonical, optimistic)

    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe('canonical-1')
  })

  it('keeps optimistic user message when canonical echo is missing', () => {
    const optimistic = [
      optimisticUserItem({
        id: 'optimistic-1',
        text: 'hello',
        timestamp: 1000,
      }),
    ]

    const merged = mergeSessionFeedItems([], optimistic)

    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe('optimistic-1')
  })
})
