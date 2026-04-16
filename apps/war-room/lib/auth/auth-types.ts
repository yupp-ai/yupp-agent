export interface SessionUser {
  id: string
  email: string
  firstName?: string
}

export interface ClientSession {
  user: {
    id: string
    firstName?: string
  }
  expiresAt: string
}

export type AuthenticatedSession = {
  status: 'authenticated'
  user: SessionUser
  expiresAt: string
}

type UnauthenticatedSession = {
  status: 'unauthenticated'
  user?: undefined
  expiresAt?: undefined
}

export type Session = AuthenticatedSession | UnauthenticatedSession
