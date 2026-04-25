import { describe, expect, it } from 'bun:test'
import { canStartSessionPageLoad } from './session-pagination'

describe('canStartSessionPageLoad', () => {
  it('returns false while a page fetch is in flight', () => {
    expect(
      canStartSessionPageLoad({
        isFetching: true,
        isLocked: false,
        loadedCount: 20,
        totalCount: 60,
      })
    ).toBe(false)
  })

  it('returns false when pagination lock is held', () => {
    expect(
      canStartSessionPageLoad({
        isFetching: false,
        isLocked: true,
        loadedCount: 20,
        totalCount: 60,
      })
    ).toBe(false)
  })

  it('returns false when loaded count already reached total', () => {
    expect(
      canStartSessionPageLoad({
        isFetching: false,
        isLocked: false,
        loadedCount: 60,
        totalCount: 60,
      })
    ).toBe(false)
  })

  it('returns true only when pagination can safely advance', () => {
    expect(
      canStartSessionPageLoad({
        isFetching: false,
        isLocked: false,
        loadedCount: 40,
        totalCount: 60,
      })
    ).toBe(true)
  })
})
