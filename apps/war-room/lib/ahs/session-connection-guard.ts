/**
 * Returns true when a connection attempt targets the currently active session.
 */
export function isSessionActive(
  activeSessionId: string | null,
  sessionId: string
): boolean {
  return activeSessionId === sessionId
}

export interface SessionConnectionAttemptInput {
  activeSessionId: string | null
  sessionId: string
  currentGeneration: number
  attemptGeneration: number
}

/**
 * Guards async websocket side-effects so stale attempts cannot mutate state.
 * A connection attempt is current only when:
 * - the hook is still on the same session id, and
 * - the attempt generation matches the latest cleanup generation.
 */
export function isCurrentSessionConnectionAttempt({
  activeSessionId,
  sessionId,
  currentGeneration,
  attemptGeneration,
}: SessionConnectionAttemptInput): boolean {
  return (
    isSessionActive(activeSessionId, sessionId) &&
    currentGeneration === attemptGeneration
  )
}
