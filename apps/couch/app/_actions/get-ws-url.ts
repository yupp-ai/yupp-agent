'use server'

import 'server-only'
import { getAhsApiKey, getAhsHost } from '@/lib/ahs/server/config'

export async function getWsUrlAction(sessionId: string): Promise<string> {
  const host = getAhsHost().replace(/^http/, 'ws').replace(/\/$/, '')
  const key = encodeURIComponent(getAhsApiKey())
  return `${host}/ahs/session/${encodeURIComponent(sessionId)}/ws?api_key=${key}`
}
