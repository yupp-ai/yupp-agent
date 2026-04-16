'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Autocomplete } from '@/components/ui/autocomplete'
import { Button, buttonVariants } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { canStartSessionPageLoad } from '@/lib/ahs/session-pagination'
import { statusColor } from '@/lib/ahs/session-status'
import {
  getSessionDisplayTitle,
  normalizeSessionTitle,
} from '@/lib/ahs/session-title'
import { AHS_SESSION_TRIGGER_VALUES } from '@/lib/ahs/session-triggers'
import type {
  AhsSessionInfo,
  AhsSessionStatus,
  AhsTriggerType,
} from '@/lib/ahs/types'
import { useTRPC } from '@/lib/hooks/trpc-client'

const PAGE_SIZE = 20

const STATUS_OPTIONS: Array<{ label: string; value: AhsSessionStatus | '' }> = [
  { label: 'All', value: '' },
  { label: 'Active', value: 'ACTIVE' },
  { label: 'Completed', value: 'COMPLETED' },
  { label: 'Stale', value: 'STALE' },
]

const TRIGGER_OPTIONS: Array<{ label: string; value: AhsTriggerType | '' }> = [
  { label: 'All', value: '' },
  { label: 'API', value: 'API' },
  { label: 'Slack', value: 'SLACK' },
  { label: 'Webhook', value: 'WEBHOOK' },
  { label: 'Cron', value: 'CRON' },
  { label: 'Task', value: 'TASK' },
]

const SESSION_TRIGGER_FILTER_VALUES = new Set(AHS_SESSION_TRIGGER_VALUES)

export default function SessionsPage() {
  const trpc = useTRPC()

  const [statusFilter, setStatusFilter] = useState<AhsSessionStatus | ''>('')
  const [triggerFilter, setTriggerFilter] = useState<AhsTriggerType | ''>('')
  const [agentFilter, setAgentFilter] = useState('')
  const [mineOnly, setMineOnly] = useState(true)
  const [offset, setOffset] = useState(0)

  // Accumulated pages for "Load More" pattern
  const [accumulated, setAccumulated] = useState<AhsSessionInfo[]>([])

  const queryInput = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset,
      include_all: !mineOnly,
      ...(statusFilter ? { status: statusFilter as AhsSessionStatus } : {}),
      ...(triggerFilter && SESSION_TRIGGER_FILTER_VALUES.has(triggerFilter)
        ? { trigger: triggerFilter as AhsTriggerType }
        : {}),
      ...(agentFilter ? { agent_name: agentFilter } : {}),
    }),
    [statusFilter, triggerFilter, agentFilter, mineOnly, offset]
  )

  const { data, isFetching, isLoading, error } = useQuery({
    ...trpc.ahs.listSessions.queryOptions(queryInput),
    placeholderData: (prev) => prev,
  })
  const { data: agents = [] } = useQuery(trpc.ahs.listAgents.queryOptions())

  const agentOptions = useMemo(
    () => [...new Set(agents.map((agent) => agent.name))],
    [agents]
  )

  // When filters change, reset accumulated. When a new page loads, append.
  const sessions = useMemo(() => {
    if (!data) return accumulated
    if (offset === 0) return data.sessions
    // Deduplicate: accumulated + new page
    const seen = new Set(accumulated.map((s) => s.session_id))
    const newSessions = data.sessions.filter((s) => !seen.has(s.session_id))
    return [...accumulated, ...newSessions]
  }, [data, offset, accumulated])

  // Sync back to accumulated whenever sessions changes
  // (we track accumulated separately so we can detect offset reset)
  const displaySessions = sessions
  const total = data?.total ?? 0
  const paginationLockRef = useRef(false)

  useEffect(() => {
    if (!isFetching) {
      paginationLockRef.current = false
    }
  }, [isFetching])

  const handleFilterChange = useCallback(() => {
    setOffset(0)
    setAccumulated([])
  }, [])

  const loadMore = useCallback(() => {
    if (
      !canStartSessionPageLoad({
        isFetching,
        isLocked: paginationLockRef.current,
        loadedCount: sessions.length,
        totalCount: total,
      })
    ) {
      return
    }

    paginationLockRef.current = true
    // Save current sessions before advancing offset
    setAccumulated(sessions)
    setOffset((prev) => prev + PAGE_SIZE)
  }, [isFetching, sessions, total])

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="font-bold text-lg tracking-tight">Sessions</h1>
        <Link className={buttonVariants({ variant: 'primary' })} href="/">
          New session
        </Link>
      </div>

      {/* Filters */}
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Label size="sm">
          Status
          <Select
            className="px-2 py-1.5"
            onValueChange={(value) => {
              setStatusFilter(value as AhsSessionStatus | '')
              handleFilterChange()
            }}
            options={STATUS_OPTIONS}
            value={statusFilter}
          />
        </Label>

        <Label size="sm">
          Trigger
          <Select
            className="px-2 py-1.5"
            onValueChange={(value) => {
              setTriggerFilter(value as AhsTriggerType | '')
              handleFilterChange()
            }}
            options={TRIGGER_OPTIONS}
            value={triggerFilter}
          />
        </Label>

        <Label size="sm">
          Agent
          <Autocomplete
            className="px-2 py-1.5"
            onValueChange={(value) => {
              setAgentFilter(value)
              handleFilterChange()
            }}
            openOnInputClick
            options={agentOptions}
            placeholder="Filter by agent..."
            value={agentFilter}
          />
        </Label>

        <Label
          htmlFor="sessions-mine-only"
          className="flex-row items-center gap-3 self-end text-sm text-zinc-300"
          size="sm"
        >
          <Switch
            checked={mineOnly}
            id="sessions-mine-only"
            onCheckedChange={(checked) => {
              setMineOnly(checked)
              handleFilterChange()
            }}
          />
          Mine only
        </Label>
      </div>

      {error && <p className="mb-4 text-sm text-red-400">{error.message}</p>}

      {!isLoading && !error && displaySessions.length === 0 && (
        <p className="text-sm text-zinc-400">No sessions found.</p>
      )}

      <div className="flex flex-col gap-2">
        {displaySessions.map((session) => (
          <SessionListItem key={session.session_id} session={session} />
        ))}
      </div>

      {displaySessions.length < total && (
        <div className="mt-4 text-center">
          <Button
            className="px-4 py-2"
            disabled={isFetching}
            onClick={loadMore}
            type="button"
          >
            {isFetching ? 'Loading...' : 'Load More'}
          </Button>
        </div>
      )}

      {isLoading && displaySessions.length === 0 && (
        <div className="flex flex-col gap-2">
          {[1, 2, 3, 4, 5].map((key) => (
            <div
              className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3"
              key={key}
            >
              <Skeleton className="h-2 w-2 rounded-full" />
              <Skeleton className="h-4 flex-1" />
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-3 w-16" />
              <Skeleton className="h-3 w-28" />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function SessionListItem({ session }: { session: AhsSessionInfo }) {
  const sessionTitle = normalizeSessionTitle(session.title)

  return (
    <Link
      className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3 transition-colors hover:bg-zinc-800/60"
      href={`/session/${session.session_id}`}
    >
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${statusColor(session.status)}`}
      />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-sm text-zinc-100">
          {getSessionDisplayTitle(session)}
        </p>
        {sessionTitle && (
          <p className="mt-1 truncate font-mono text-xs text-zinc-500">
            {session.session_id}
          </p>
        )}
      </div>
      <span className="text-xs text-zinc-400">{session.agent_name}</span>
      <span className="text-xs text-zinc-500">{session.status}</span>
      <span className="text-xs text-zinc-600">
        {session.created_at
          ? new Date(session.created_at).toLocaleString()
          : '—'}
      </span>
    </Link>
  )
}
