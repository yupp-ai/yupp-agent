'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { ScheduleForm } from '@/components/schedules/schedule-form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  AHS_SCHEDULE_STATUS_VALUES,
  AHS_SCHEDULE_TYPE_VALUES,
  type AhsScheduleInfo,
  type AhsScheduleStatus,
  type AhsScheduleType,
} from '@/lib/ahs/types'
import { useSession } from '@/lib/auth/session-provider'
import { useTRPC } from '@/lib/hooks/trpc-client'

const STATUS_OPTIONS = [
  { label: 'All statuses', value: '' },
  ...AHS_SCHEDULE_STATUS_VALUES.map((value) => ({ label: value, value })),
]

const TYPE_OPTIONS = [
  { label: 'All types', value: '' },
  ...AHS_SCHEDULE_TYPE_VALUES.map((value) => ({ label: value, value })),
]

function formatDateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function getScheduleTimingLabel(schedule: AhsScheduleInfo): string {
  if (schedule.schedule_type === 'RECURRING') {
    return `Next run ${formatDateTime(schedule.next_run_at)}`
  }

  return `Executes ${formatDateTime(schedule.execute_at)}`
}

function getScheduleSummary(schedule: AhsScheduleInfo): string {
  if (schedule.schedule_type === 'RECURRING') {
    return schedule.cron_expression
      ? `${schedule.cron_expression} (${schedule.cron_timezone ?? 'UTC'})`
      : 'Recurring automation'
  }

  return 'One-time automation'
}

function statusBadgeClass(status: AhsScheduleStatus): string {
  switch (status) {
    case 'PENDING':
      return 'bg-amber-900/40 text-amber-100'
    case 'IN_PROGRESS':
      return 'bg-blue-900/40 text-blue-100'
    case 'COMPLETED':
      return 'bg-emerald-900/40 text-emerald-100'
    case 'FAILED':
      return 'bg-red-900/40 text-red-100'
    case 'CANCELLED':
      return 'bg-zinc-800 text-zinc-300'
    case 'PAUSED':
      return 'bg-purple-900/40 text-purple-100'
  }
}

function formatMutationError(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }

  return 'Something went wrong. Please try again.'
}

function addPendingId(current: string[], scheduleId: string): string[] {
  return current.includes(scheduleId) ? current : [...current, scheduleId]
}

function removePendingId(current: string[], scheduleId: string): string[] {
  return current.filter((currentId) => currentId !== scheduleId)
}

export default function SchedulesPage() {
  const trpc = useTRPC()
  const queryClient = useQueryClient()
  const session = useSession()
  const [statusFilter, setStatusFilter] = useState<AhsScheduleStatus | ''>('')
  const [typeFilter, setTypeFilter] = useState<AhsScheduleType | ''>('')
  const [agentFilter, setAgentFilter] = useState('')
  const [mineOnly, setMineOnly] = useState(true)
  const [isCreateFormOpen, setIsCreateFormOpen] = useState(false)
  const [selectedScheduleId, setSelectedScheduleId] = useState<string | null>(
    null
  )
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [pendingTriggerScheduleIds, setPendingTriggerScheduleIds] = useState<
    string[]
  >([])
  const [pendingCancelScheduleIds, setPendingCancelScheduleIds] = useState<
    string[]
  >([])

  const queryInput = useMemo(
    () => ({
      limit: 100,
      mine_only: mineOnly,
      ...(statusFilter ? { status: statusFilter } : {}),
      ...(typeFilter ? { schedule_type: typeFilter } : {}),
      ...(agentFilter ? { agent_name: agentFilter } : {}),
    }),
    [agentFilter, mineOnly, statusFilter, typeFilter]
  )

  const {
    data: schedulesData,
    error,
    isLoading,
  } = useQuery({
    ...trpc.ahs.listSchedules.queryOptions(queryInput),
    placeholderData: (previous) => previous,
  })

  const { data: agents = [] } = useQuery(trpc.ahs.listAgents.queryOptions())

  const triggerScheduleMutation = useMutation(
    trpc.ahs.triggerSchedule.mutationOptions({
      onMutate({ agent_schedule_id }) {
        setActionError(null)
        setPendingTriggerScheduleIds((current) =>
          addPendingId(current, agent_schedule_id)
        )
      },
      async onSuccess(result) {
        await queryClient.invalidateQueries(trpc.ahs.listSchedules.pathFilter())
        setNotice(
          `Triggered automation ${result.agent_schedule_id}. Session ${result.session_id} is running.`
        )
        setActionError(null)
      },
      onError(error) {
        setActionError(formatMutationError(error))
      },
      onSettled(_data, _error, { agent_schedule_id }) {
        setPendingTriggerScheduleIds((current) =>
          removePendingId(current, agent_schedule_id)
        )
      },
    })
  )

  const cancelScheduleMutation = useMutation(
    trpc.ahs.cancelSchedule.mutationOptions({
      onMutate({ agent_schedule_id }) {
        setActionError(null)
        setPendingCancelScheduleIds((current) =>
          addPendingId(current, agent_schedule_id)
        )
      },
      async onSuccess(result) {
        await queryClient.invalidateQueries(trpc.ahs.listSchedules.pathFilter())
        setNotice(`Cancelled automation ${result.agent_schedule_id}.`)
        setActionError(null)
        if (selectedScheduleId === result.agent_schedule_id) {
          setSelectedScheduleId(null)
        }
      },
      onError(error) {
        setActionError(formatMutationError(error))
      },
      onSettled(_data, _error, { agent_schedule_id }) {
        setPendingCancelScheduleIds((current) =>
          removePendingId(current, agent_schedule_id)
        )
      },
    })
  )

  const schedules = schedulesData?.schedules ?? []
  const agentOptions = agents.map((agent) => ({
    label: agent.display_name,
    value: agent.name,
  }))
  const isEditFormOpen = selectedScheduleId !== null

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-bold text-lg tracking-tight">Automations</h1>
          <p className="mt-1 text-sm text-zinc-500">
            Create and update your agent automations, then manage owner-only
            actions.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => {
              setActionError(null)
              setNotice(null)
              setIsCreateFormOpen(true)
            }}
            type="button"
            variant="primary"
          >
            New automation
          </Button>
        </div>
      </div>

      {notice && <p className="mb-4 text-sm text-emerald-400">{notice}</p>}
      {actionError && (
        <p className="mb-4 text-sm text-red-400">{actionError}</p>
      )}

      <Dialog onOpenChange={setIsCreateFormOpen} open={isCreateFormOpen}>
        <DialogContent
          className="max-w-5xl overflow-hidden border border-white/15 bg-transparent p-0 shadow-none"
          showCloseButton={false}
        >
          <DialogTitle className="sr-only">New automation</DialogTitle>
          <DialogDescription className="sr-only">
            Create a one-time or recurring automation for an agent.
          </DialogDescription>
          <ScheduleForm
            agentOptions={agentOptions}
            mode="create"
            onCancel={() => setIsCreateFormOpen(false)}
            onSuccess={(scheduleId) => {
              setNotice(`Created automation ${scheduleId}.`)
              setIsCreateFormOpen(false)
            }}
          />
        </DialogContent>
      </Dialog>

      <Dialog
        onOpenChange={(open) => {
          if (!open) {
            setSelectedScheduleId(null)
          }
        }}
        open={isEditFormOpen}
      >
        <DialogContent
          className="max-w-5xl overflow-hidden border border-white/15 bg-transparent p-0 shadow-none"
          showCloseButton={false}
        >
          <DialogTitle className="sr-only">Edit automation</DialogTitle>
          <DialogDescription className="sr-only">
            Update an existing automation.
          </DialogDescription>
          {selectedScheduleId && (
            <ScheduleForm
              agentOptions={agentOptions}
              mode="edit"
              onCancel={() => setSelectedScheduleId(null)}
              onSuccess={(scheduleId) => {
                setNotice(`Saved changes to automation ${scheduleId}.`)
                setSelectedScheduleId(null)
              }}
              scheduleId={selectedScheduleId}
            />
          )}
        </DialogContent>
      </Dialog>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Filters</CardTitle>
          <CardDescription>
            You can only edit automations you created while they are PENDING or
            PAUSED.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-4">
          <Label>
            Status
            <Select
              onValueChange={(value) =>
                setStatusFilter(value as AhsScheduleStatus | '')
              }
              options={STATUS_OPTIONS}
              value={statusFilter}
            />
          </Label>

          <Label>
            Type
            <Select
              onValueChange={(value) =>
                setTypeFilter(value as AhsScheduleType | '')
              }
              options={TYPE_OPTIONS}
              value={typeFilter}
            />
          </Label>

          <Label>
            Agent name
            <Input
              list="schedule-agent-options"
              onChange={(event) => setAgentFilter(event.target.value)}
              placeholder="Filter by agent"
              value={agentFilter}
            />
            <datalist id="schedule-agent-options">
              {agentOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </datalist>
          </Label>

          <Label
            className="flex-row items-center gap-3 self-end text-sm text-zinc-300"
            htmlFor="schedules-mine-only"
          >
            <Switch
              checked={mineOnly}
              id="schedules-mine-only"
              onCheckedChange={setMineOnly}
            />
            Mine only
          </Label>
        </CardContent>
      </Card>

      {error && <p className="mb-4 text-sm text-red-400">{error.message}</p>}

      {isLoading && (
        <div className="grid gap-4 lg:grid-cols-2">
          {['1', '2', '3', '4'].map((key) => (
            <Card key={key}>
              <CardHeader>
                <Skeleton className="h-6 w-48" />
                <Skeleton className="h-4 w-64" />
              </CardHeader>
              <CardContent className="space-y-3">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-20 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {!isLoading && !error && schedules.length === 0 && (
        <p className="text-sm text-zinc-400">No automations found.</p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {schedules.map((schedule) => {
          const isOwner = schedule.created_by_user === session.user.id
          const canEdit =
            isOwner &&
            (schedule.status === 'PENDING' || schedule.status === 'PAUSED')
          const canTrigger =
            isOwner &&
            schedule.schedule_type === 'RECURRING' &&
            schedule.status === 'PENDING'
          const canCancel =
            isOwner &&
            (schedule.status === 'PENDING' || schedule.status === 'PAUSED')
          const isTriggerPending = pendingTriggerScheduleIds.includes(
            schedule.agent_schedule_id
          )
          const isCancelPending = pendingCancelScheduleIds.includes(
            schedule.agent_schedule_id
          )

          return (
            <Card key={schedule.agent_schedule_id}>
              <CardHeader>
                <CardAction className="flex gap-2">
                  {canEdit && (
                    <Button
                      onClick={() => {
                        setActionError(null)
                        setNotice(null)
                        setSelectedScheduleId(schedule.agent_schedule_id)
                      }}
                      size="sm"
                      type="button"
                    >
                      Edit
                    </Button>
                  )}
                </CardAction>
                <div className="flex flex-wrap items-center gap-2">
                  <CardTitle>{schedule.name ?? schedule.agent_name}</CardTitle>
                  <Badge className={statusBadgeClass(schedule.status)}>
                    {schedule.status}
                  </Badge>
                  <Badge>{schedule.schedule_type}</Badge>
                </div>
                <CardDescription>
                  {schedule.description ?? 'No description'}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3 text-sm text-zinc-300">
                <p className="text-zinc-400">
                  {getScheduleTimingLabel(schedule)}
                </p>
                <p className="font-mono text-xs text-zinc-500">
                  {getScheduleSummary(schedule)}
                </p>
                <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-xs text-zinc-300">
                  {schedule.message}
                </div>
              </CardContent>
              <CardFooter className="flex flex-wrap gap-2 justify-between">
                <div className="flex flex-wrap gap-2 text-xs text-zinc-500">
                  <span>agent {schedule.agent_name}</span>
                  <span>runs {schedule.run_count}</span>
                  <span>created {formatDateTime(schedule.created_at)}</span>
                  {schedule.created_by_agent && (
                    <span>via {schedule.created_by_agent}</span>
                  )}
                </div>
                <div className="flex gap-2">
                  {canTrigger && (
                    <Button
                      disabled={isTriggerPending}
                      onClick={() =>
                        triggerScheduleMutation.mutate({
                          agent_schedule_id: schedule.agent_schedule_id,
                        })
                      }
                      size="sm"
                      type="button"
                      variant="primary"
                    >
                      {isTriggerPending ? 'Running...' : 'Run now'}
                    </Button>
                  )}
                  {canCancel && (
                    <Button
                      disabled={isCancelPending}
                      onClick={() =>
                        cancelScheduleMutation.mutate({
                          agent_schedule_id: schedule.agent_schedule_id,
                        })
                      }
                      size="sm"
                      type="button"
                      variant="destructive"
                    >
                      {isCancelPending ? 'Cancelling...' : 'Cancel'}
                    </Button>
                  )}
                </div>
              </CardFooter>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
