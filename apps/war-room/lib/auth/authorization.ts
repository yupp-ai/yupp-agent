import type { SessionUser } from './auth-types'

function isYuppEmail(email: string): boolean {
  return email.toLowerCase().endsWith('@yupp.ai')
}

export function canAccessWarRoom(user: Pick<SessionUser, 'email'>) {
  return isYuppEmail(user.email)
}

export function canAccessWarRoomDuringLogin(input: {
  email: string
  emailVerified?: boolean
}) {
  return input.emailVerified === true && isYuppEmail(input.email)
}
