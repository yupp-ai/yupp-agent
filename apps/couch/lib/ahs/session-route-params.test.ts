import { describe, expect, it } from 'bun:test'
import {
  getSideBySideSessionIds,
  isSessionRouteIdValid,
} from './session-route-params'

describe('isSessionRouteIdValid', () => {
  it('accepts ordinary session ids', () => {
    expect(isSessionRouteIdValid('session-123')).toBe(true)
  })

  it('rejects blank or reserved route ids', () => {
    expect(isSessionRouteIdValid('')).toBe(false)
    expect(isSessionRouteIdValid('   ')).toBe(false)
    expect(isSessionRouteIdValid('new')).toBe(false)
  })
})

describe('getSideBySideSessionIds', () => {
  it('returns trimmed left and right ids when both are valid', () => {
    expect(
      getSideBySideSessionIds({
        left: ' left-session ',
        right: 'right-session ',
      })
    ).toEqual({
      left: 'left-session',
      right: 'right-session',
    })
  })

  it('rejects duplicate session ids', () => {
    expect(
      getSideBySideSessionIds({
        left: 'same-session',
        right: 'same-session',
      })
    ).toBe(null)
  })

  it('rejects reserved or blank ids', () => {
    expect(
      getSideBySideSessionIds({
        left: 'new',
        right: 'session-2',
      })
    ).toBe(null)
    expect(
      getSideBySideSessionIds({
        left: 'session-1',
        right: '   ',
      })
    ).toBe(null)
  })
})
