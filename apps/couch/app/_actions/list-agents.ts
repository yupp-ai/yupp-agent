'use server'

import 'server-only'
import { listAgents } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function listAgentsAction() {
  const session = await getInternalSession()
  if (session.status === 'unauthenticated') {
    throw new Error('Unauthorized')
  }
  return listAgents({
    user_id: session.user.id,
    include_all: false,
  })
}
