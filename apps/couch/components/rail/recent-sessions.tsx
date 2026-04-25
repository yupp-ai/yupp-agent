'use client'

import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { listMyRecentSessionsAction } from '@/app/_actions/list-sessions'
import { SessionRow, type SessionRowData } from './session-row'

const PIN_KEY = 'couch.pinned'
const RENAME_KEY = 'couch.rename'

function loadJSON<T>(key: string, fallback: T): T {
  if (typeof window === 'undefined') return fallback
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    return JSON.parse(raw) as T
  } catch {
    return fallback
  }
}

export function RecentSessions({ activeId }: { activeId?: string }) {
  const [pinned, setPinned] = useState<Record<string, boolean>>({})
  const [renames, setRenames] = useState<Record<string, string>>({})

  useEffect(() => {
    setPinned(loadJSON(PIN_KEY, {}))
    setRenames(loadJSON(RENAME_KEY, {}))
  }, [])

  const { data } = useQuery({
    queryKey: ['recent-sessions'],
    queryFn: () => listMyRecentSessionsAction({ limit: 20 }),
    refetchInterval: 30_000,
  })

  const all: SessionRowData[] = (data?.sessions ?? []) as SessionRowData[]
  const sorted = [...all].sort((a, b) => {
    const ap = pinned[a.session_id] ? 1 : 0
    const bp = pinned[b.session_id] ? 1 : 0
    if (ap !== bp) return bp - ap
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  })

  function togglePin(id: string) {
    setPinned((prev) => {
      const next = { ...prev, [id]: !prev[id] }
      localStorage.setItem(PIN_KEY, JSON.stringify(next))
      return next
    })
  }
  function rename(id: string, value: string) {
    setRenames((prev) => {
      const next = { ...prev }
      const trimmed = value.trim()
      if (trimmed) next[id] = trimmed
      else delete next[id]
      localStorage.setItem(RENAME_KEY, JSON.stringify(next))
      return next
    })
  }

  return (
    <div className="flex flex-col gap-0.5">
      {sorted.map((s) => (
        <SessionRow
          active={s.session_id === activeId}
          customTitle={renames[s.session_id]}
          data={s}
          key={s.session_id}
          onRename={(v) => rename(s.session_id, v)}
          onTogglePin={() => togglePin(s.session_id)}
          pinned={pinned[s.session_id]}
        />
      ))}
    </div>
  )
}
