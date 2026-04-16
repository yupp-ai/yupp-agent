import type { AhsMessageHistoryItem, AhsToolUse } from './types'

export interface AhsSessionToolUse extends AhsToolUse {
  assistant_message: string | null
  created_at: string | null
  message_id: string
  turn_number: number
}

export function collectSessionToolUses(
  messages: AhsMessageHistoryItem[]
): AhsSessionToolUse[] {
  return messages.flatMap((message) =>
    message.tool_uses.map((toolUse) => ({
      ...toolUse,
      assistant_message: message.content,
      created_at: message.created_at,
      message_id: message.message_id,
      turn_number: message.turn_number,
    }))
  )
}

export function formatToolUseValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (value == null) return 'null'

  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
