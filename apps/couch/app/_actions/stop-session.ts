'use server'

import 'server-only'
import { getInternalSession } from '@/lib/auth/get-session'
import { stopSession } from '@/lib/ahs/server/client'

export async function stopSessionAction(sessionId: string) {
  const auth = await getInternalSession()
  if (auth.status === 'unauthenticated') throw new Error('Unauthorized')
  return stopSession(sessionId)
}
