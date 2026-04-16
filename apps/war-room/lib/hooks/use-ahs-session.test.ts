import * as bunTest from 'bun:test'
import type { AhsServerEvent } from '@/lib/ahs/types'

const { describe, expect, it } = bunTest

;(
  bunTest as unknown as {
    mock: { module(path: string, factory: () => unknown): void }
  }
).mock.module('@yupp/agents-protocol/stream-items', () => ({
  applyStreamItemActions: (items: unknown[]) => items,
}))

async function loadHookHelpers() {
  return await import('./use-ahs-session')
}

describe('useAhsSession stream translation', () => {
  it('sets itemId for user message items', async () => {
    const { createAhsStreamTranslationState, translateAhsServerEvent } =
      await loadHookHelpers()
    const state = createAhsStreamTranslationState()
    const event: AhsServerEvent = {
      type: 'item/started',
      event_id: 'event-1',
      item: { id: 'ahs-item-1', type: 'user_message', text: 'hello' },
    }

    const actions = translateAhsServerEvent(event, state)

    expect(actions).toHaveLength(1)
    expect(actions[0]).toMatchObject({
      type: 'create',
      item: {
        itemId: 'ahs-item-1',
        type: 'message',
        status: 'complete',
        data: { role: 'user', text: 'hello' },
      },
    })
  })

  it('sets itemId for agent message items', async () => {
    const { createAhsStreamTranslationState, translateAhsServerEvent } =
      await loadHookHelpers()
    const state = createAhsStreamTranslationState()
    const event: AhsServerEvent = {
      type: 'item/started',
      event_id: 'event-2',
      item: { id: 'ahs-item-2', type: 'agent_message' },
    }

    const actions = translateAhsServerEvent(event, state)

    expect(actions).toHaveLength(1)
    expect(actions[0]).toMatchObject({
      type: 'create',
      item: {
        itemId: 'ahs-item-2',
        type: 'message',
        status: 'streaming',
        data: { role: 'assistant', text: '' },
      },
    })
  })

  it('supports current slash-style stream event names', async () => {
    const {
      createAhsStreamTranslationState,
      translateAhsServerEvent,
      finalizeStreamingToolItemsForTurn,
    } = await loadHookHelpers()
    const state = createAhsStreamTranslationState()
    const started: AhsServerEvent = {
      type: 'item/started',
      event_id: 1,
      thread_id: 'thread-1',
      turn_id: 'thread-1:1',
      item: { id: 'ahs-item-3', type: 'agent_message', text: '' },
    }
    const delta: AhsServerEvent = {
      type: 'item/agentMessage/delta',
      event_id: 2,
      thread_id: 'thread-1',
      turn_id: 'thread-1:1',
      item_id: 'ahs-item-3',
      delta: 'Hello from AHS',
    }

    const startedActions = translateAhsServerEvent(started, state)
    const deltaActions = translateAhsServerEvent(delta, state)

    expect(startedActions).toHaveLength(1)
    expect(startedActions[0]).toMatchObject({
      type: 'create',
      item: {
        itemId: 'ahs-item-3',
        type: 'message',
        status: 'streaming',
        data: { role: 'assistant', text: '' },
      },
    })
    expect(deltaActions).toHaveLength(1)
    expect(deltaActions[0]).toEqual({
      type: 'append_text',
      id: (startedActions[0] as { item: { id: string } }).item.id,
      text: 'Hello from AHS',
    })

    const toolStart: AhsServerEvent = {
      type: 'item/started',
      event_id: 3,
      thread_id: 'thread-1',
      turn_id: 'thread-1:1',
      item: {
        id: 'ahs-tool-1',
        type: 'mcp_tool_call',
        server: 'ahs',
        tool: 'Read',
        arguments: { path: 'README.md' },
      },
    }
    const toolActions = translateAhsServerEvent(toolStart, state)

    expect(toolActions).toHaveLength(1)
    expect(toolActions[0]).toMatchObject({
      type: 'create',
      item: {
        itemId: 'ahs-tool-1',
        type: 'mcp_tool_call',
        status: 'streaming',
        threadId: 'thread-1',
        turnId: 'thread-1:1',
      },
    })

    const finalized = finalizeStreamingToolItemsForTurn(
      [
        (
          toolActions[0] as {
            type: 'create'
            item: {
              id: string
              itemId: string
              type: 'mcp_tool_call'
              status: 'streaming'
              timestamp: number
              threadId: string
              turnId: string
              data: Record<string, unknown>
            }
          }
        ).item,
      ],
      { turnId: 'thread-1:1', status: 'complete' }
    )

    expect(finalized[0]).toMatchObject({
      itemId: 'ahs-tool-1',
      type: 'mcp_tool_call',
      status: 'complete',
      threadId: 'thread-1',
      turnId: 'thread-1:1',
    })
  })

  it('treats failed slash-style turn completion as an error item', async () => {
    const { createAhsStreamTranslationState, translateAhsServerEvent } =
      await loadHookHelpers()
    const state = createAhsStreamTranslationState()
    const event: AhsServerEvent = {
      type: 'turn/completed',
      event_id: 3,
      turn_id: 'thread-1:1',
      status: 'failed',
      error: { message: 'Turn cancelled by user' },
    }

    const actions = translateAhsServerEvent(event, state)

    expect(actions).toHaveLength(1)
    expect(actions[0]).toMatchObject({
      type: 'create',
      item: {
        type: 'error',
        status: 'error',
        turnId: 'thread-1:1',
        data: { message: 'Turn cancelled by user' },
      },
    })
  })

  it('only finalizes streaming tool items for the completed turn', async () => {
    const { finalizeStreamingToolItemsForTurn } = await loadHookHelpers()

    const finalized = finalizeStreamingToolItemsForTurn(
      [
        {
          id: 'tool-1',
          itemId: 'ahs-tool-1',
          type: 'mcp_tool_call',
          status: 'streaming',
          timestamp: 1,
          turnId: 'turn-1',
          data: {},
        },
        {
          id: 'tool-2',
          itemId: 'ahs-tool-2',
          type: 'command_execution',
          status: 'streaming',
          timestamp: 2,
          turnId: 'turn-2',
          data: {},
        },
      ],
      { turnId: 'turn-1', status: 'complete' }
    )

    expect(finalized[0]?.status).toBe('complete')
    expect(finalized[1]?.status).toBe('streaming')
  })

  it('does not finalize streaming tool items when turnId is missing', async () => {
    const { finalizeStreamingToolItemsForTurn } = await loadHookHelpers()

    const finalized = finalizeStreamingToolItemsForTurn(
      [
        {
          id: 'tool-1',
          itemId: 'ahs-tool-1',
          type: 'mcp_tool_call',
          status: 'streaming',
          timestamp: 1,
          turnId: 'turn-1',
          data: {},
        },
      ],
      { status: 'complete' }
    )

    expect(finalized[0]?.status).toBe('streaming')
  })
})
