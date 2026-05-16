import type { SessionUser } from './auth-types'

// Couch is internal but does not enforce a domain allowlist here — anyone
// with a verified Google account who is also registered in the AHS users
// database (checked via resolveUser in the OAuth callback) passes.
// Tighten this when Couch is exposed outside the team.

export function canAccessCouch(_user: Pick<SessionUser, 'email'>) {
  return true
}

export function canAccessCouchDuringLogin(input: {
  email: string
  emailVerified?: boolean
}) {
  return input.emailVerified === true
}
