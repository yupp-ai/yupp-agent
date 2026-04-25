'use server'

import 'server-only'
import { redirect } from 'next/navigation'
import { createSession } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function createSessionAction(input: {
  agent_id: string
  message: string
}) {
  const session = await getInternalSession()
  if (session.status === 'unauthenticated') {
    throw new Error('Unauthorized')
  }
  const created = await createSession({
    agent_id: input.agent_id,
    trigger: 'api',
    message: input.message,
    user_id: session.user.id,
  })
  redirect(`/s/${created.session_id}`)
}
