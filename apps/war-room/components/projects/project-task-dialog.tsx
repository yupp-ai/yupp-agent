'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { type ReactNode, useState } from 'react'
import {
  dependencyLabel,
  formatDateTime,
  formatMoney,
  priorityBadgeClass,
  taskStatusBadgeClass,
} from '@/components/projects/project-utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { AhsTaskResponse, AhsTaskStatusValue } from '@/lib/ahs/types'
import { useTRPC } from '@/lib/hooks/trpc-client'

type ProjectTaskDialogProps = {
  initialTask: AhsTaskResponse
  open: boolean
  onOpenChange: (open: boolean) => void
  projectId: string
}

type TaskAction = {
  confirmationMessage?: string
  key: string
  kind: 'resume' | 'status'
  label: string
  status?: AhsTaskStatusValue
  variant?: 'default' | 'destructive' | 'primary'
}

function formatMutationError(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }

  return 'Something went wrong. Please try again.'
}

function stringifyJson(value: unknown): string {
  if (value == null) {
    return ''
  }

  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function DetailItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="space-y-1">
      <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">
        {label}
      </p>
      <div className="text-sm text-zinc-200">{value}</div>
    </div>
  )
}

function JsonBlock({
  content,
  emptyState,
}: {
  content: string
  emptyState: string
}) {
  if (!content.trim()) {
    return <p className="text-sm text-zinc-500">{emptyState}</p>
  }

  return (
    <pre className="overflow-x-auto rounded-xl border border-zinc-800 bg-zinc-950/80 p-4 font-mono text-xs leading-6 text-zinc-300 whitespace-pre-wrap">
      {content}
    </pre>
  )
}

function codeValue(value: string) {
  return (
    <span className="font-mono text-xs text-zinc-300 whitespace-pre-wrap">
      {value}
    </span>
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function canResumeTask(task: AhsTaskResponse): boolean {
  if ((task.assigned_session_ids?.length ?? 0) === 0) {
    return false
  }

  if (!isRecord(task.task_data)) {
    return false
  }

  return task.task_data.error_subtype === 'error_max_turns'
}

function sessionLinks(sessionIds: string[] | null): ReactNode {
  if (!sessionIds?.length) {
    return 'None'
  }

  return (
    <div className="flex flex-wrap gap-2">
      {sessionIds.map((sessionId) => (
        <Link
          className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-300 transition-colors hover:text-zinc-100"
          href={`/session/${encodeURIComponent(sessionId)}`}
          key={sessionId}
        >
          {sessionId}
        </Link>
      ))}
    </div>
  )
}

function dependencyValues(dependsOn: string[] | null): ReactNode {
  if (!dependsOn?.length) {
    return 'None'
  }

  return (
    <div className="flex flex-wrap gap-2">
      {dependsOn.map((dependencyId) => (
        <span
          className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-300"
          key={dependencyId}
        >
          {dependencyId}
        </span>
      ))}
    </div>
  )
}

function getTaskActions(task: AhsTaskResponse): TaskAction[] {
  const hasDependencies = (task.depends_on?.length ?? 0) > 0

  switch (task.status) {
    case 'PENDING':
      return [
        ...(hasDependencies
          ? [
              {
                key: 'status:BLOCKED',
                kind: 'status' as const,
                label: 'Mark blocked',
                status: 'BLOCKED' as const,
                confirmationMessage:
                  'Move this task to BLOCKED? You can change it again later.',
              },
            ]
          : []),
        {
          key: 'status:READY',
          kind: 'status',
          label: 'Mark ready',
          status: 'READY',
          variant: 'primary',
        },
        {
          key: 'status:CANCELLED',
          kind: 'status',
          label: 'Cancel',
          status: 'CANCELLED',
          confirmationMessage:
            'Cancel this task? You can restart it later from the task view.',
          variant: 'destructive',
        },
      ]
    case 'BLOCKED':
      return [
        {
          key: 'status:READY',
          kind: 'status',
          label: 'Mark ready',
          status: 'READY',
          variant: 'primary',
        },
        {
          key: 'status:PENDING',
          kind: 'status',
          label: 'Move to pending',
          status: 'PENDING',
        },
        {
          key: 'status:CANCELLED',
          kind: 'status',
          label: 'Cancel',
          status: 'CANCELLED',
          confirmationMessage:
            'Cancel this task? You can restart it later from the task view.',
          variant: 'destructive',
        },
      ]
    case 'READY':
      return [
        {
          key: 'status:IN_PROGRESS',
          kind: 'status',
          label: 'Start',
          status: 'IN_PROGRESS',
          variant: 'primary',
        },
        ...(hasDependencies
          ? [
              {
                key: 'status:BLOCKED',
                kind: 'status' as const,
                label: 'Block',
                status: 'BLOCKED' as const,
                confirmationMessage:
                  'Move this task to BLOCKED? You can change it again later.',
              },
            ]
          : []),
        {
          key: 'status:PENDING',
          kind: 'status',
          label: 'Move to pending',
          status: 'PENDING',
        },
        {
          key: 'status:CANCELLED',
          kind: 'status',
          label: 'Cancel',
          status: 'CANCELLED',
          confirmationMessage:
            'Cancel this task? You can restart it later from the task view.',
          variant: 'destructive',
        },
      ]
    case 'IN_PROGRESS':
      return [
        {
          key: 'status:IN_REVIEW',
          kind: 'status',
          label: 'Send to review',
          status: 'IN_REVIEW',
          variant: 'primary',
        },
        {
          key: 'status:COMPLETED',
          kind: 'status',
          label: 'Complete',
          status: 'COMPLETED',
          variant: 'primary',
        },
        {
          key: 'status:FAILED',
          kind: 'status',
          label: 'Mark failed',
          status: 'FAILED',
          confirmationMessage:
            'Mark this task as FAILED? You can move it back to READY or PENDING later.',
          variant: 'destructive',
        },
        {
          key: 'status:READY',
          kind: 'status',
          label: 'Move to ready',
          status: 'READY',
        },
        {
          key: 'status:PENDING',
          kind: 'status',
          label: 'Move to pending',
          status: 'PENDING',
        },
        {
          key: 'status:CANCELLED',
          kind: 'status',
          label: 'Cancel',
          status: 'CANCELLED',
          confirmationMessage:
            'Cancel this task? You can restart it later from the task view.',
          variant: 'destructive',
        },
      ]
    case 'IN_REVIEW':
      return [
        {
          key: 'status:COMPLETED',
          kind: 'status',
          label: 'Complete',
          status: 'COMPLETED',
          variant: 'primary',
        },
        {
          key: 'status:FAILED',
          kind: 'status',
          label: 'Mark failed',
          status: 'FAILED',
          confirmationMessage:
            'Mark this task as FAILED? You can move it back to READY or PENDING later.',
          variant: 'destructive',
        },
        {
          key: 'status:PENDING',
          kind: 'status',
          label: 'Move to pending',
          status: 'PENDING',
        },
        {
          key: 'status:CANCELLED',
          kind: 'status',
          label: 'Cancel',
          status: 'CANCELLED',
          confirmationMessage:
            'Cancel this task? You can restart it later from the task view.',
          variant: 'destructive',
        },
      ]
    case 'COMPLETED':
      return [
        {
          key: 'status:PENDING',
          kind: 'status',
          label: 'Restart',
          status: 'PENDING',
        },
        {
          key: 'status:FAILED',
          kind: 'status',
          label: 'Mark failed',
          status: 'FAILED',
          confirmationMessage:
            'Mark this task as FAILED? You can move it back to READY or PENDING later.',
          variant: 'destructive',
        },
      ]
    case 'FAILED':
      return [
        ...(canResumeTask(task)
          ? [
              {
                key: 'resume',
                kind: 'resume' as const,
                label: 'Resume',
                variant: 'primary' as const,
              },
            ]
          : []),
        {
          key: 'status:PENDING',
          kind: 'status',
          label: 'Restart',
          status: 'PENDING',
        },
        {
          key: 'status:READY',
          kind: 'status',
          label: 'Mark ready',
          status: 'READY',
        },
      ]
    case 'CANCELLED':
      return [
        {
          key: 'status:PENDING',
          kind: 'status',
          label: 'Restart',
          status: 'PENDING',
        },
      ]
  }
}

export function ProjectTaskDialog({
  initialTask,
  open,
  onOpenChange,
  projectId,
}: ProjectTaskDialogProps) {
  const trpc = useTRPC()
  const queryClient = useQueryClient()
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [pendingActionKey, setPendingActionKey] = useState<string | null>(null)
  const taskId = initialTask.agent_task_id

  const taskQuery = useQuery({
    ...trpc.ahs.getProjectTask.queryOptions({ projectId, taskId }),
    placeholderData: () => initialTask,
  })

  const task = taskQuery.data ?? initialTask
  const actions = getTaskActions(task)
  const dependencyText = dependencyLabel(task) ?? 'No dependencies'
  const isActionPending = pendingActionKey !== null
  const isTaskResumable = canResumeTask(task)

  async function invalidateTaskData() {
    await Promise.all([
      queryClient.invalidateQueries(trpc.ahs.getProject.pathFilter()),
      queryClient.invalidateQueries(trpc.ahs.getProjectTask.pathFilter()),
      queryClient.invalidateQueries(trpc.ahs.listProjects.pathFilter()),
      queryClient.invalidateQueries(trpc.ahs.listProjectTasks.pathFilter()),
    ])
  }

  const setTaskStatusMutation = useMutation(
    trpc.ahs.setProjectTaskStatus.mutationOptions({
      onMutate({ status }) {
        setActionError(null)
        setNotice(null)
        setPendingActionKey(`status:${status}`)
      },
      async onSuccess(result) {
        await invalidateTaskData()
        const readyCount = result.newly_ready_tasks.length
        setNotice(
          readyCount > 0
            ? `Task moved to ${result.status}. ${readyCount} dependent ${readyCount === 1 ? 'task is' : 'tasks are'} now READY.`
            : `Task moved to ${result.status}.`
        )
      },
      onError(error) {
        setActionError(formatMutationError(error))
      },
      onSettled() {
        setPendingActionKey(null)
      },
    })
  )

  const resumeTaskMutation = useMutation(
    trpc.ahs.resumeProjectTask.mutationOptions({
      onMutate() {
        setActionError(null)
        setNotice(null)
        setPendingActionKey('resume')
      },
      async onSuccess(result) {
        await invalidateTaskData()
        setNotice(
          result.session_to_resume
            ? `Task resumed. Session ${result.session_to_resume} is ready to continue.`
            : `Task resumed and moved to ${result.status}.`
        )
      },
      onError(error) {
        setActionError(formatMutationError(error))
      },
      onSettled() {
        setPendingActionKey(null)
      },
    })
  )

  function handleAction(action: TaskAction) {
    if (
      action.confirmationMessage &&
      !window.confirm(action.confirmationMessage)
    ) {
      return
    }

    if (action.kind === 'resume') {
      resumeTaskMutation.mutate({ projectId, taskId })
      return
    }

    if (!action.status) {
      return
    }

    setTaskStatusMutation.mutate({
      projectId,
      taskId,
      status: action.status,
    })
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-5xl">
        <DialogHeader className="pr-16">
          <p className="font-mono text-xs uppercase tracking-[0.18em] text-zinc-500">
            Task view
          </p>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="space-y-2">
              <p className="font-mono text-xs text-zinc-500">{taskId}</p>
              <DialogTitle className="text-xl text-zinc-50">
                {task.title}
              </DialogTitle>
              <DialogDescription className="max-w-3xl">
                {task.description ?? 'No description provided.'}
              </DialogDescription>
            </div>
            <div className="flex flex-wrap gap-2">
              <Badge className={taskStatusBadgeClass(task.status)}>
                {task.status}
              </Badge>
              <Badge className={priorityBadgeClass(task.priority)}>
                {task.priority}
              </Badge>
            </div>
          </div>
        </DialogHeader>

        <div className="mt-6 space-y-4">
          {notice && <p className="text-sm text-emerald-400">{notice}</p>}
          {actionError && <p className="text-sm text-red-400">{actionError}</p>}
          {taskQuery.error && (
            <p className="text-sm text-amber-400">
              Could not refresh the latest task data: {taskQuery.error.message}
            </p>
          )}

          <Card>
            <CardHeader>
              <CardTitle>Actions</CardTitle>
              <CardDescription>
                Run supported AHS task actions directly from this view.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-wrap gap-2">
                {actions.map((action) => (
                  <Button
                    disabled={isActionPending}
                    key={action.key}
                    onClick={() => handleAction(action)}
                    size="sm"
                    type="button"
                    variant={action.variant}
                  >
                    {pendingActionKey === action.key
                      ? `${action.label}…`
                      : action.label}
                  </Button>
                ))}
              </div>
              <p className="text-xs text-zinc-500">
                Resume depends on upstream task state and may still be rejected
                by AHS if the task is not resumable.
              </p>
              {task.status === 'FAILED' && !isTaskResumable && (
                <p className="text-xs text-zinc-500">
                  This failed task is not resumable through AHS, so restart and
                  manual status actions are shown instead.
                </p>
              )}
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Details</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-4 sm:grid-cols-2">
                <DetailItem
                  label="Assigned agent"
                  value={task.agent_name ?? 'Unassigned'}
                />
                <DetailItem
                  label="Creator"
                  value={task.creator_user_name ?? task.creator_user_id ?? '—'}
                />
                <DetailItem
                  label="Created"
                  value={formatDateTime(task.created_at)}
                />
                <DetailItem
                  label="Completed"
                  value={formatDateTime(task.completed_at)}
                />
                <DetailItem
                  label="Estimated effort"
                  value={task.estimated_effort ?? '—'}
                />
                <DetailItem
                  label="Actual spend"
                  value={formatMoney(task.actual_spending_usd)}
                />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Relationships</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <DetailItem
                  label="Project"
                  value={codeValue(task.agent_project_id)}
                />
                <DetailItem
                  label="Parent task"
                  value={
                    task.parent_task_id ? codeValue(task.parent_task_id) : '—'
                  }
                />
                <DetailItem label="Dependencies" value={dependencyText} />
                <DetailItem
                  label="Dependency IDs"
                  value={dependencyValues(task.depends_on)}
                />
                <DetailItem
                  label="Assigned sessions"
                  value={sessionLinks(task.assigned_session_ids)}
                />
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Result</CardTitle>
              <CardDescription>
                Stored completion or failure payload returned by AHS.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <JsonBlock
                content={stringifyJson(task.result)}
                emptyState="No result recorded."
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Task data</CardTitle>
              <CardDescription>
                Additional task metadata carried through execution.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <JsonBlock
                content={stringifyJson(task.task_data)}
                emptyState="No task data available."
              />
            </CardContent>
          </Card>
        </div>
      </DialogContent>
    </Dialog>
  )
}
