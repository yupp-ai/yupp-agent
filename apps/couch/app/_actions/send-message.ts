'use server'

import 'server-only'
import { getInternalSession } from '@/lib/auth/get-session'
import { sendMessage } from '@/lib/ahs/server/client'

export async function sendMessageAction(input: {
  sessionId: string
  message: string
  source?: 'api' | 'websocket'
}) {
  const auth = await getInternalSession()
  if (auth.status === 'unauthenticated') throw new Error('Unauthorized')
  return sendMessage({
    session_id: input.sessionId,
    message: input.message,
    user_id: auth.user.id,
    source: input.source ?? 'api',
  })
}
