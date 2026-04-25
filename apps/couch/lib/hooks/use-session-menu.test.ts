import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { readMenuState, writeMenuState } from './use-session-menu'

interface FakeStorage extends Storage {
  _store: Record<string, string>
}

function installFakeLocalStorage(): FakeStorage {
  const fake: FakeStorage = {
    _store: {},
    getItem(k: string) {
      return this._store[k] ?? null
    },
    setItem(k: string, v: string) {
      this._store[k] = v
    },
    removeItem(k: string) {
      delete this._store[k]
    },
    clear() {
      this._store = {}
    },
    key() {
      return null
    },
    length: 0,
  }
  ;(globalThis as { localStorage?: Storage }).localStorage = fake
  return fake
}

describe('session menu state', () => {
  beforeEach(() => {
    installFakeLocalStorage()
  })
  afterEach(() => {
    delete (globalThis as { localStorage?: Storage }).localStorage
  })

  test('default state has tool calls visible and not pinned', () => {
    const s = readMenuState('abc')
    expect(s.toolCallsVisible).toBe(true)
    expect(s.pinned).toBe(false)
    expect(s.customTitle).toBeUndefined()
  })

  test('writeMenuState round-trips toolCallsVisible', () => {
    writeMenuState('abc', { toolCallsVisible: false })
    expect(readMenuState('abc').toolCallsVisible).toBe(false)
  })

  test('writeMenuState merges fields across calls', () => {
    writeMenuState('abc', { toolCallsVisible: false })
    writeMenuState('abc', { pinned: true })
    const s = readMenuState('abc')
    expect(s.toolCallsVisible).toBe(false)
    expect(s.pinned).toBe(true)
  })

  test('per-session isolation', () => {
    writeMenuState('abc', { customTitle: 'A' })
    writeMenuState('def', { customTitle: 'D' })
    expect(readMenuState('abc').customTitle).toBe('A')
    expect(readMenuState('def').customTitle).toBe('D')
  })
})
