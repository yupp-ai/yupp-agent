import type { AhsSessionInfo } from './types'

type SessionTitleSource = Pick<AhsSessionInfo, 'session_id' | 'title'>

export function normalizeSessionTitle(
  title: string | null | undefined
): string | null {
  const normalizedTitle = title?.trim()
  return normalizedTitle ? normalizedTitle : null
}

export function getSessionDisplayTitle(session: SessionTitleSource): string {
  return normalizeSessionTitle(session.title) ?? session.session_id
}
