import type { SessionUser } from './auth-types'

// Couch is internal but does not enforce a domain allowlist — anyone
// with a verified Google account passes. Tighten this when we expose
// Couch outside the team. Function names mirror war-room's so the
// callsites in get-session.ts and the OAuth callback don't need to
// change; rename in a later cleanup if useful.

export function canAccessWarRoom(_user: Pick<SessionUser, 'email'>) {
  return true
}

export function canAccessWarRoomDuringLogin(input: {
  email: string
  emailVerified?: boolean
}) {
  return input.emailVerified === true
}
