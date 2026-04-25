import { describe, expect, test } from 'bun:test'
import { resolveArtifactRefetchInterval } from './artifact-refresh'

describe('resolveArtifactRefetchInterval', () => {
  test('5s while a turn is running', () => {
    expect(resolveArtifactRefetchInterval(true)).toBe(5_000)
  })
  test('30s when idle', () => {
    expect(resolveArtifactRefetchInterval(false)).toBe(30_000)
  })
})
