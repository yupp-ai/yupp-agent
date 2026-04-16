import 'server-only'

import { cookies } from 'next/headers'
import { cache } from 'react'
import type { AuthenticatedSession, Session, SessionUser } from './auth-types'
import { canAccessWarRoom } from './authorization'
import {
  type SessionCookiePayload,
  unsafeGetSessionFromCookie,
} from './session-cookie'

export type InternalAuthenticatedSession = {
  status: 'authenticated'
  user: SessionUser
  expiresAt: Date
}

export type InternalSession =
  | InternalAuthenticatedSession
  | {
      status: 'unauthenticated'
      user?: undefined
    }

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

  if (!canAccessWarRoom(user)) {
    return { status: 'unauthenticated' }
  }

  return {
    status: 'authenticated',
    expiresAt,
    user,
  }
})
