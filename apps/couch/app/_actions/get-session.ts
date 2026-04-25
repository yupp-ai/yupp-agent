'use server'

import 'server-only'
import { getSession as getAhsSession } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function getSessionAction(sessionId: string) {
  const auth = await getInternalSession()
  if (auth.status === 'unauthenticated') {
    throw new Error('Unauthorized')
  }
  return getAhsSession(sessionId)
}
