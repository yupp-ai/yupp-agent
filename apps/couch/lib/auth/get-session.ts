import 'server-only'

import { cookies } from 'next/headers'
import { cache } from 'react'
import type { AuthenticatedSession, Session, SessionUser } from './auth-types'
import { canAccessCouch } from './authorization'
import { isLocalDevelopment } from './environments'
import { type SessionCookiePayload, unsafeGetSessionFromCookie } from './session-cookie'

// Dev-only auth bypass. Set in .env.local:
//   COUCH_DEV_BYPASS_AUTH_EMAIL=test@example.com
//   COUCH_DEV_BYPASS_AUTH_USER_ID=00000000-0000-0000-0000-000000000000
// Only honored when NODE_ENV=development.
function tryDevBypass(): InternalAuthenticatedSession | null {
  if (!isLocalDevelopment) return null
  const email = process.env.COUCH_DEV_BYPASS_AUTH_EMAIL
  if (!email) return null
  const userId =
    process.env.COUCH_DEV_BYPASS_AUTH_USER_ID ?? '00000000-0000-0000-0000-000000000000'
  return {
    status: 'authenticated',
    user: { id: userId, email, firstName: 'Test' },
    expiresAt: new Date(Date.now() + 1000 * 60 * 60 * 24),
  }
}

export type InternalAuthenticatedSession = {
  status: 'authenticated'
  user: SessionUser
  expiresAt: Date
}

export type InternalSession =
  | InternalAuthenticatedSession
  | { status: 'unauthenticated'; user?: undefined }

function createSessionUser(input: {
  userId: string
  email: string
  firstName?: string
}): SessionUser {
  return {
    id: input.userId,
    email: input.email,
    firstName: input.firstName,
  }
}

function sanitizeSession(session: InternalSession): Session {
  if (session.status === 'authenticated') {
    return {
      status: 'authenticated',
      expiresAt: session.expiresAt.toISOString(),
      user: session.user,
    } satisfies AuthenticatedSession
  }
  return { status: 'unauthenticated' }
}

export async function getSession(): Promise<Session> {
  return sanitizeSession(await getInternalSession())
}

export const getInternalSession = cache(async (): Promise<InternalSession> => {
  const bypass = tryDevBypass()
  if (bypass) return bypass

  const cookieStore = await cookies()

  let payload: SessionCookiePayload | null = null
  try {
    payload = await unsafeGetSessionFromCookie(cookieStore)
  } catch {
    return { status: 'unauthenticated' }
  }

  if (!payload) {
    return { status: 'unauthenticated' }
  }

  const expiresAt = new Date(payload.expiresAt)
  if (Number.isNaN(expiresAt.getTime()) || expiresAt <= new Date()) {
    return { status: 'unauthenticated' }
  }

  const user = createSessionUser({
    userId: payload.userId,
    email: payload.email,
    firstName: payload.firstName,
  })

  if (!canAccessCouch(user)) {
    return { status: 'unauthenticated' }
  }

  return {
    status: 'authenticated',
    expiresAt,
    user,
  }
})
