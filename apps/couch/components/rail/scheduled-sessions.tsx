'use client'

import { useQuery } from '@tanstack/react-query'
import { listMyRecentSessionsAction } from '@/app/_actions/list-sessions'
import { SessionRow, type SessionRowData } from './session-row'

export function ScheduledSessions({ activeId }: { activeId?: string }) {
  const { data } = useQuery({
    queryKey: ['scheduled-sessions'],
    queryFn: () => listMyRecentSessionsAction({ limit: 50 }),
    refetchInterval: 30_000,
  })

  const cron: SessionRowData[] = (
    (data?.sessions ?? []) as SessionRowData[]
  ).filter((s) => s.trigger === 'CRON')
  const sorted = [...cron].sort(
    (a, b) =>
      new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  )

  if (sorted.length === 0) {
    return (
      <p className="px-2 py-3 text-muted-foreground text-xs">
        No scheduled runs yet.
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-0.5">
      {sorted.map((s) => (
        <SessionRow
          active={s.session_id === activeId}
          data={s}
          key={s.session_id}
          onRename={() => {
            /* not editable for scheduled rows */
          }}
          onTogglePin={() => {
            /* not pinnable for scheduled rows */
          }}
        />
      ))}
    </div>
  )
}
