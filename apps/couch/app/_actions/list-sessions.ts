'use server'

import 'server-only'
import { listSessions } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function listMyRecentSessionsAction(
  opts: {
    limit?: number
    offset?: number
  } = {}
) {
  const session = await getInternalSession()
  if (session.status === 'unauthenticated') {
    throw new Error('Unauthorized')
  }
  return listSessions({
    user_id: session.user.id,
    include_all: false,
    limit: opts.limit ?? 20,
    offset: opts.offset ?? 0,
    root_sessions_only: true,
  })
}
