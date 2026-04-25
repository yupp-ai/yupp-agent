import { describe, expect, test } from 'bun:test'
import { formatBytes, relativeTime } from './format'

describe('relativeTime', () => {
  const NOW = new Date('2026-04-24T12:00:00Z').getTime()

  test('seconds ago', () => {
    expect(relativeTime(new Date(NOW - 30_000), NOW)).toBe('just now')
  })
  test('minutes ago', () => {
    expect(relativeTime(new Date(NOW - 5 * 60_000), NOW)).toBe('5 minutes ago')
    expect(relativeTime(new Date(NOW - 60_000), NOW)).toBe('1 minute ago')
  })
  test('hours ago', () => {
    expect(relativeTime(new Date(NOW - 4 * 3600_000), NOW)).toBe('4 hours ago')
  })
  test('days ago', () => {
    expect(relativeTime(new Date(NOW - 2 * 86_400_000), NOW)).toBe('2 days ago')
  })
  test('over a week renders absolute date', () => {
    expect(relativeTime(new Date('2026-04-01T12:00:00Z'), NOW)).toMatch(/Apr 1/)
  })
})

describe('formatBytes', () => {
  test('bytes', () => {
    expect(formatBytes(512)).toBe('512 B')
  })
  test('kilobytes', () => {
    expect(formatBytes(2048)).toBe('2.0 KB')
  })
  test('megabytes', () => {
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})
