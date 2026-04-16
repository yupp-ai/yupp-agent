import { describe, expect, it } from 'bun:test'
import {
  isCurrentSessionConnectionAttempt,
  isSessionActive,
} from './session-connection-guard'

describe('session connection guard', () => {
  it('recognizes the active session id', () => {
    expect(isSessionActive('session-1', 'session-1')).toBe(true)
    expect(isSessionActive('session-1', 'session-2')).toBe(false)
    expect(isSessionActive(null, 'session-1')).toBe(false)
  })

  it('accepts only attempts for the active session and generation', () => {
    expect(
      isCurrentSessionConnectionAttempt({
        activeSessionId: 'session-1',
        sessionId: 'session-1',
        currentGeneration: 3,
        attemptGeneration: 3,
      })
    ).toBe(true)
  })

  it('rejects stale attempts from previous session ids', () => {
    expect(
      isCurrentSessionConnectionAttempt({
        activeSessionId: 'session-2',
        sessionId: 'session-1',
        currentGeneration: 3,
        attemptGeneration: 3,
      })
    ).toBe(false)
  })

  it('rejects stale attempts from previous generations', () => {
    expect(
      isCurrentSessionConnectionAttempt({
        activeSessionId: 'session-1',
        sessionId: 'session-1',
        currentGeneration: 4,
        attemptGeneration: 3,
      })
    ).toBe(false)
  })
})
