'use server'

import 'server-only'
import { getSessionHistory } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function getSessionHistoryAction(
  sessionId: string,
  opts: {
    limit?: number
    offset?: number
  } = {}
) {
  const auth = await getInternalSession()
  if (auth.status === 'unauthenticated') {
    throw new Error('Unauthorized')
  }
  return getSessionHistory(sessionId, opts.limit ?? 200, opts.offset ?? 0)
}
