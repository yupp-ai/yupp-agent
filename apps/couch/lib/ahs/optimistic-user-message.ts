import type { StreamItem } from '@yupp/agents-ui/types'

export const OPTIMISTIC_USER_ITEM_ID_PREFIX = 'optimistic-user:'

function isMessageItem(item: StreamItem): boolean {
  return item.type === 'message'
}

function getMessageData(item: StreamItem): Record<string, unknown> | null {
  if (!isMessageItem(item) || !item.data) {
    return null
  }

  return item.data
}

function getRole(item: StreamItem): string | null {
  const data = getMessageData(item)
  if (!data) return null
  const role = data.role
  return typeof role === 'string' ? role : null
}

export function getMessageText(item: StreamItem): string | null {
  const data = getMessageData(item)
  if (!data) return null
  const text = data.text
  return typeof text === 'string' ? text : null
}

export function isOptimisticUserMessageItem(item: StreamItem): boolean {
  if (!isMessageItem(item)) return false
  if (getRole(item) !== 'user') return false
  return (
    typeof item.itemId === 'string' &&
    item.itemId.startsWith(OPTIMISTIC_USER_ITEM_ID_PREFIX)
  )
}
