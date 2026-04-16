import { describe, expect, it } from 'bun:test'
import { getSessionDisplayTitle, normalizeSessionTitle } from './session-title'

describe('normalizeSessionTitle', () => {
  it('returns null for empty titles', () => {
    expect(normalizeSessionTitle('')).toBe(null)
    expect(normalizeSessionTitle('   ')).toBe(null)
    expect(normalizeSessionTitle(null)).toBe(null)
    expect(normalizeSessionTitle(undefined)).toBe(null)
  })

  it('trims valid titles', () => {
    expect(normalizeSessionTitle('  Triage prod incident  ')).toBe(
      'Triage prod incident'
    )
  })
})

describe('getSessionDisplayTitle', () => {
  it('returns the normalized title when present', () => {
    expect(
      getSessionDisplayTitle({
        session_id: 'session-123',
        title: '  Follow up with support  ',
      })
    ).toBe('Follow up with support')
  })

  it('falls back to the session id when title is missing', () => {
    expect(
      getSessionDisplayTitle({
        session_id: 'session-123',
        title: '   ',
      })
    ).toBe('session-123')
  })
})
