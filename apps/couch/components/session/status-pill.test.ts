import { describe, expect, test } from 'bun:test'
import { resolveStatus } from './status-pill'

describe('resolveStatus', () => {
  test('completed session → Stopped', () => {
    expect(
      resolveStatus({
        sessionStatus: 'COMPLETED',
        wsStatus: 'connected',
        turnInFlight: false,
      }).label
    ).toBe('Stopped')
  })
  test('active + turn in flight → Working', () => {
    expect(
      resolveStatus({
        sessionStatus: 'ACTIVE',
        wsStatus: 'connected',
        turnInFlight: true,
      }).label
    ).toBe('Working…')
  })
  test('active + idle → Awaiting instructions', () => {
    expect(
      resolveStatus({
        sessionStatus: 'ACTIVE',
        wsStatus: 'connected',
        turnInFlight: false,
      }).label
    ).toBe('Couch is awaiting instructions')
  })
  test('reconnecting ws → Reconnecting', () => {
    expect(
      resolveStatus({
        sessionStatus: 'ACTIVE',
        wsStatus: 'reconnecting',
        turnInFlight: false,
      }).label
    ).toBe('Reconnecting…')
  })
  test('error ws → error tone', () => {
    expect(
      resolveStatus({
        sessionStatus: 'ACTIVE',
        wsStatus: 'error',
        turnInFlight: false,
      }).tone
    ).toBe('error')
  })
})
