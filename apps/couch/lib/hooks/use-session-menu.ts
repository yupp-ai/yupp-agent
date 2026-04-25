'use client'

import { useEffect, useState } from 'react'

export interface SessionMenuState {
  toolCallsVisible: boolean
  pinned: boolean
  customTitle?: string
}

const KEY = 'couch.sessionMenu'

const DEFAULT_STATE: SessionMenuState = {
  toolCallsVisible: true,
  pinned: false,
}

function getStorage(): Storage | null {
  if (typeof globalThis === 'undefined') return null
  const ls = (globalThis as { localStorage?: Storage }).localStorage
  return ls ?? null
}

function loadAll(): Record<string, SessionMenuState> {
  const ls = getStorage()
  if (!ls) return {}
  try {
    const raw = ls.getItem(KEY)
    return raw ? (JSON.parse(raw) as Record<string, SessionMenuState>) : {}
  } catch {
    return {}
  }
}

function saveAll(data: Record<string, SessionMenuState>) {
  const ls = getStorage()
  if (!ls) return
  ls.setItem(KEY, JSON.stringify(data))
}

export function readMenuState(sessionId: string): SessionMenuState {
  const all = loadAll()
  return all[sessionId] ?? DEFAULT_STATE
}

export function writeMenuState(
  sessionId: string,
  patch: Partial<SessionMenuState>
): SessionMenuState {
  const all = loadAll()
  const current = all[sessionId] ?? DEFAULT_STATE
  const next = { ...current, ...patch }
  all[sessionId] = next
  saveAll(all)
  return next
}

export function useSessionMenu(sessionId: string) {
  const [state, setState] = useState<SessionMenuState>(DEFAULT_STATE)
  useEffect(() => {
    setState(readMenuState(sessionId))
  }, [sessionId])
  function update(patch: Partial<SessionMenuState>) {
    const next = writeMenuState(sessionId, patch)
    setState(next)
  }
  return [state, update] as const
}
