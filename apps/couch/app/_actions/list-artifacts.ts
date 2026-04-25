'use server'

import 'server-only'
import { listArtifacts } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function listArtifactsAction(sessionId: string) {
  const auth = await getInternalSession()
  if (auth.status === 'unauthenticated') {
    throw new Error('Unauthorized')
  }
  const result = await listArtifacts({
    agent_session_id: sessionId,
    limit: 200,
  })
  return result.artifacts
}
