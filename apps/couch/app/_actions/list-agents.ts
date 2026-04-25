'use server'

import 'server-only'
import { listAgents } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function listAgentsAction() {
  const session = await getInternalSession()
  if (session.status === 'unauthenticated') {
    throw new Error('Unauthorized')
  }
  // include_all=true so the picker shows every agent the user can talk to,
  // not only ones they personally created (most users have created none).
  return listAgents({ include_all: true })
}
