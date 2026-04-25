import { describe, expect, it } from 'bun:test'
import { collectSessionToolUses, formatToolUseValue } from './session-tool-uses'
import type { AhsMessageHistoryItem } from './types'

const messages: AhsMessageHistoryItem[] = [
  {
    message_id: 'msg-1',
    turn_number: 3,
    role: 'assistant',
    content: 'Running a command',
    tool_uses: [
      {
        tool_use_id: 'tool-1',
        name: 'bash',
        input: { cmd: 'pwd' },
        output: '/tmp',
        is_error: false,
        duration_ms: 25,
        step: 1,
      },
    ],
    cost_usd: 0.02,
    duration_ms: 100,
    num_agent_turns: 1,
    slack_ts: null,
    created_at: '2026-03-06T12:00:00Z',
  },
]

describe('session tool-use helpers', () => {
  it('flattens tool uses while preserving message metadata', () => {
    expect(collectSessionToolUses(messages)).toEqual([
      {
        tool_use_id: 'tool-1',
        name: 'bash',
        input: { cmd: 'pwd' },
        output: '/tmp',
        is_error: false,
        duration_ms: 25,
        step: 1,
        assistant_message: 'Running a command',
        created_at: '2026-03-06T12:00:00Z',
        message_id: 'msg-1',
        turn_number: 3,
      },
    ])
  })

  it('formats structured values as pretty JSON', () => {
    expect(formatToolUseValue({ hello: 'world' })).toBe(
      JSON.stringify({ hello: 'world' }, null, 2)
    )
  })

  it('passes strings through without re-encoding', () => {
    expect(formatToolUseValue('plain output')).toBe('plain output')
  })
})
