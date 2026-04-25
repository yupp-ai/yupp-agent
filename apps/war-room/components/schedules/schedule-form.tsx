'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, type SelectOption } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import type {
  AhsScheduleDetailResponse,
  AhsScheduleType,
} from '@/lib/ahs/types'
import {
  parseOptionalJsonRecord,
  stringifyJsonValue,
  trimToUndefined,
} from '@/lib/form-utils'
import { useTRPC } from '@/lib/hooks/trpc-client'

const SCHEDULE_TYPE_OPTIONS: SelectOption[] = [
  { label: 'One-time', value: 'SCHEDULED' },
  { label: 'Recurring', value: 'RECURRING' },
]

type BaseScheduleFormProps = {
  agentOptions: SelectOption[]
  onCancel: () => void
  onSuccess: (scheduleId: string) => void
}

type CreateScheduleFormProps = BaseScheduleFormProps & {
  mode: 'create'
}

type EditScheduleFormProps = BaseScheduleFormProps & {
  mode: 'edit'
  scheduleId: string
}

type ScheduleFormProps = CreateScheduleFormProps | EditScheduleFormProps

type ScheduleFormState = {
  scheduleType: AhsScheduleType
  agentName: string
  name: string
  description: string
  message: string
  executeAt: string
  cronExpression: string
  timezone: string
  maxRuns: string
  context: string
}

function createDefaultFormState(initialAgentName = ''): ScheduleFormState {
  return {
    scheduleType: 'SCHEDULED',
    agentName: initialAgentName,
    name: '',
    description: '',
    message: '',
    executeAt: '',
    cronExpression: '',
    timezone: 'UTC',
    maxRuns: '',
    context: '',
  }
}

function createFormStateFromDetail(
  detail: AhsScheduleDetailResponse
): ScheduleFormState {
  return {
    scheduleType: detail.schedule.schedule_type,
    agentName: detail.schedule.agent_name,
    name: detail.schedule.name ?? '',
    description: detail.schedule.description ?? '',
    message: detail.schedule.message,
    executeAt: detail.schedule.execute_at
      ? detail.schedule.execute_at.slice(0, 16)
      : '',
    cronExpression: detail.schedule.cron_expression ?? '',
    timezone: detail.schedule.cron_timezone ?? 'UTC',
    maxRuns:
      detail.schedule.max_runs == null ? '' : String(detail.schedule.max_runs),
    context: stringifyJsonValue(detail.schedule.context),
  }
}

function formatDateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function formatMutationError(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }

  return 'Something went wrong. Please try again.'
}

function getScheduleTypeLabel(scheduleType: AhsScheduleType): string {
  return scheduleType === 'RECURRING' ? 'Recurring' : 'One-time'
}

export function ScheduleForm(props: ScheduleFormProps) {
  const trpc = useTRPC()
  const queryClient = useQueryClient()
  const [formState, setFormState] = useState<ScheduleFormState>(() =>
    createDefaultFormState(props.agentOptions[0]?.value ?? '')
  )
  const [formError, setFormError] = useState<string | null>(null)

  const scheduleQuery = useQuery({
    ...trpc.ahs.getSchedule.queryOptions({
      id: props.mode === 'edit' ? props.scheduleId : '',
    }),
    enabled: props.mode === 'edit',
  })

  useEffect(() => {
    if (props.mode === 'edit' && scheduleQuery.data) {
      setFormState(createFormStateFromDetail(scheduleQuery.data))
      setFormError(null)
    }
  }, [props.mode, scheduleQuery.data])

  useEffect(() => {
    if (
      props.mode === 'create' &&
      !formState.agentName &&
      props.agentOptions[0]?.value
    ) {
      setFormState((current) => ({
        ...current,
        agentName: props.agentOptions[0]?.value ?? '',
      }))
    }
  }, [formState.agentName, props.agentOptions, props.mode])

  const createScheduleMutation = useMutation(
    trpc.ahs.createSchedule.mutationOptions({
      async onSuccess(result) {
        await queryClient.invalidateQueries(trpc.ahs.listSchedules.pathFilter())
        props.onSuccess(result.agent_schedule_id)
      },
      onError(error) {
        setFormError(formatMutationError(error))
      },
    })
  )

  const createRecurringScheduleMutation = useMutation(
    trpc.ahs.createRecurringSchedule.mutationOptions({
      async onSuccess(result) {
        await queryClient.invalidateQueries(trpc.ahs.listSchedules.pathFilter())
        props.onSuccess(result.agent_schedule_id)
      },
      onError(error) {
        setFormError(formatMutationError(error))
      },
    })
  )

  const editScheduleMutation = useMutation(
    trpc.ahs.editSchedule.mutationOptions({
      async onSuccess(result) {
        await Promise.all([
          queryClient.invalidateQueries(trpc.ahs.listSchedules.pathFilter()),
          queryClient.invalidateQueries(trpc.ahs.getSchedule.pathFilter()),
        ])
        props.onSuccess(result.agent_schedule_id)
      },
      onError(error) {
        setFormError(formatMutationError(error))
      },
    })
  )

  const schedule = scheduleQuery.data?.schedule
  const isRecurring = formState.scheduleType === 'RECURRING'
  const isPending =
    createScheduleMutation.isPending ||
    createRecurringScheduleMutation.isPending ||
    editScheduleMutation.isPending

  const title =
    props.mode === 'create'
      ? isRecurring
        ? 'New recurring automation'
        : 'New one-time automation'
      : `Edit ${schedule?.name ?? schedule?.agent_name ?? 'automation'}`

  const description =
    props.mode === 'create'
      ? isRecurring
        ? 'Create a recurring automation for an agent with a cron cadence.'
        : 'Create a one-time automation for an agent.'
      : schedule?.schedule_type === 'RECURRING'
        ? 'Update the recurring automation cadence and prompt.'
        : 'Update the automation metadata and prompt.'

  const scheduleTimingLabel = useMemo(() => {
    if (!schedule) {
      return ''
    }

    return schedule.schedule_type === 'RECURRING'
      ? `Next run: ${formatDateTime(schedule.next_run_at)}`
      : `Execute at: ${formatDateTime(schedule.execute_at)}`
  }, [schedule])

  function resetForm() {
    if (props.mode === 'create') {
      setFormState(createDefaultFormState(props.agentOptions[0]?.value ?? ''))
      setFormError(null)
      return
    }

    if (scheduleQuery.data) {
      setFormState(createFormStateFromDetail(scheduleQuery.data))
      setFormError(null)
    }
  }

  function updateField<Key extends keyof ScheduleFormState>(
    key: Key,
    value: ScheduleFormState[Key]
  ) {
    setFormState((current) => ({ ...current, [key]: value }))
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)

    const parsedContext = parseOptionalJsonRecord(formState.context, 'Context')
    if (parsedContext.error) {
      setFormError(parsedContext.error)
      return
    }

    const maxRunsValue =
      formState.maxRuns.trim().length > 0
        ? Number(formState.maxRuns)
        : undefined

    if (
      maxRunsValue !== undefined &&
      (!Number.isInteger(maxRunsValue) || maxRunsValue <= 0)
    ) {
      setFormError('Max runs must be a positive whole number.')
      return
    }

    const message = trimToUndefined(formState.message)
    if (!message) {
      setFormError('Prompt message is required.')
      return
    }

    const timezone = trimToUndefined(formState.timezone) ?? 'UTC'

    if (props.mode === 'create') {
      const agentName = trimToUndefined(formState.agentName)
      if (!agentName) {
        setFormError('Select an agent before creating an automation.')
        return
      }

      if (!isRecurring) {
        const executeAt = trimToUndefined(formState.executeAt)
        if (!executeAt) {
          setFormError('Execution time is required for one-time automations.')
          return
        }

        createScheduleMutation.mutate({
          agent_name: agentName,
          message,
          execute_at: executeAt,
          timezone,
          name: trimToUndefined(formState.name),
          description: trimToUndefined(formState.description),
          ...(parsedContext.value ? { context: parsedContext.value } : {}),
        })
        return
      }

      const cronExpression = trimToUndefined(formState.cronExpression)
      if (!cronExpression) {
        setFormError('Cron expression is required for recurring automations.')
        return
      }

      createRecurringScheduleMutation.mutate({
        agent_name: agentName,
        message,
        cron_expression: cronExpression,
        timezone,
        name: trimToUndefined(formState.name),
        description: trimToUndefined(formState.description),
        ...(parsedContext.value ? { context: parsedContext.value } : {}),
        ...(maxRunsValue !== undefined ? { max_runs: maxRunsValue } : {}),
      })
      return
    }

    editScheduleMutation.mutate({
      agent_schedule_id: props.scheduleId,
      message,
      name: trimToUndefined(formState.name),
      description: trimToUndefined(formState.description),
      ...(parsedContext.value ? { context: parsedContext.value } : {}),
      ...(isRecurring
        ? {
            cron_expression: trimToUndefined(formState.cronExpression),
            timezone,
            ...(maxRunsValue !== undefined ? { max_runs: maxRunsValue } : {}),
          }
        : {}),
    })
  }

  if (props.mode === 'edit' && scheduleQuery.isLoading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-64" />
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          {['1', '2', '3', '4'].map((key) => (
            <Skeleton className="h-16" key={key} />
          ))}
          <Skeleton className="h-40 md:col-span-2" />
        </CardContent>
      </Card>
    )
  }

  if (props.mode === 'edit' && scheduleQuery.error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Unable to load automation</CardTitle>
          <CardDescription>
            The automation details could not be loaded for editing.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-red-400">{scheduleQuery.error.message}</p>
        </CardContent>
        <CardFooter className="justify-end gap-2">
          <Button onClick={() => void scheduleQuery.refetch()} type="button">
            Retry
          </Button>
          <Button onClick={props.onCancel} type="button">
            Close
          </Button>
        </CardFooter>
      </Card>
    )
  }

  if (props.mode === 'edit' && !schedule) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Automation not found</CardTitle>
          <CardDescription>
            War Room could not find the selected automation.
          </CardDescription>
        </CardHeader>
        <CardFooter className="justify-end">
          <Button onClick={props.onCancel} type="button">
            Close
          </Button>
        </CardFooter>
      </Card>
    )
  }

  const submitLabel =
    props.mode === 'create'
      ? isPending
        ? 'Creating...'
        : 'New automation'
      : isPending
        ? 'Saving...'
        : 'Save changes'

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <form className="space-y-6" onSubmit={handleSubmit}>
          <div className="grid gap-4 md:grid-cols-2">
            {props.mode === 'create' ? (
              <>
                <Label>
                  Automation type
                  <Select
                    onValueChange={(value) =>
                      updateField('scheduleType', value as AhsScheduleType)
                    }
                    options={SCHEDULE_TYPE_OPTIONS}
                    value={formState.scheduleType}
                  />
                </Label>

                <Label>
                  Agent
                  <Select
                    onValueChange={(value) => updateField('agentName', value)}
                    options={props.agentOptions}
                    placeholder={
                      props.agentOptions.length > 0
                        ? 'Select agent'
                        : 'No agents available'
                    }
                    value={formState.agentName}
                  />
                </Label>
              </>
            ) : (
              <>
                <Label>
                  Automation type
                  <Input
                    disabled
                    value={getScheduleTypeLabel(formState.scheduleType)}
                  />
                </Label>

                <Label>
                  Agent
                  <Input disabled value={schedule?.agent_name ?? ''} />
                </Label>
              </>
            )}

            <Label>
              Name
              <Input
                onChange={(event) => updateField('name', event.target.value)}
                value={formState.name}
              />
            </Label>

            <Label>
              Description
              <Input
                onChange={(event) =>
                  updateField('description', event.target.value)
                }
                value={formState.description}
              />
            </Label>

            <Label className="md:col-span-2">
              Prompt message
              <Textarea
                className="min-h-40"
                onChange={(event) => updateField('message', event.target.value)}
                value={formState.message}
              />
            </Label>

            {isRecurring ? (
              <>
                <Label>
                  Cron expression
                  <Input
                    onChange={(event) =>
                      updateField('cronExpression', event.target.value)
                    }
                    placeholder="0 9 * * 1-5"
                    value={formState.cronExpression}
                  />
                </Label>

                <Label>
                  Timezone
                  <Input
                    onChange={(event) =>
                      updateField('timezone', event.target.value)
                    }
                    placeholder="America/Los_Angeles"
                    value={formState.timezone}
                  />
                </Label>

                <Label>
                  Max runs
                  <Input
                    min="1"
                    onChange={(event) =>
                      updateField('maxRuns', event.target.value)
                    }
                    placeholder="Leave blank for unlimited"
                    type="number"
                    value={formState.maxRuns}
                  />
                </Label>

                {props.mode === 'edit' ? (
                  <div className="self-end text-sm text-zinc-400">
                    {scheduleTimingLabel}
                  </div>
                ) : (
                  <div className="self-end text-sm text-zinc-400">
                    Cron is evaluated in the selected timezone.
                  </div>
                )}
              </>
            ) : props.mode === 'create' ? (
              <>
                <Label>
                  Execute at
                  <Input
                    onChange={(event) =>
                      updateField('executeAt', event.target.value)
                    }
                    type="datetime-local"
                    value={formState.executeAt}
                  />
                </Label>

                <Label>
                  Timezone
                  <Input
                    onChange={(event) =>
                      updateField('timezone', event.target.value)
                    }
                    placeholder="America/Los_Angeles"
                    value={formState.timezone}
                  />
                </Label>
              </>
            ) : (
              <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 text-sm text-zinc-400 md:col-span-2">
                <p>{scheduleTimingLabel}</p>
                {/* TODO: Add execute_at to POST /ahs/schedule/edit for SCHEDULED jobs.
                    War Room can only update shared metadata until one-time schedules can be
                    rescheduled through the AHS edit endpoint. */}
                <p className="mt-2 text-xs text-zinc-500">
                  One-time automations cannot be rescheduled from War Room yet.
                  That requires `execute_at` support in the AHS schedule edit
                  endpoint.
                </p>
              </div>
            )}
          </div>

          <Label>
            Context JSON
            <Textarea
              className="min-h-40 font-mono text-xs"
              onChange={(event) => updateField('context', event.target.value)}
              placeholder="Leave blank for no context. Use `{}` to send an empty object."
              value={formState.context}
            />
          </Label>

          {props.mode === 'create' && props.agentOptions.length === 0 && (
            <p className="text-sm text-red-400">
              No agents are available. Create an agent first.
            </p>
          )}

          {formError && <p className="text-sm text-red-400">{formError}</p>}

          <CardFooter className="justify-end gap-2 px-0">
            <Button
              onClick={() => {
                resetForm()
                props.onCancel()
              }}
              type="button"
            >
              Cancel
            </Button>
            <Button
              disabled={
                isPending ||
                (props.mode === 'create' && props.agentOptions.length === 0)
              }
              type="submit"
              variant="primary"
            >
              {submitLabel}
            </Button>
          </CardFooter>
        </form>
      </CardContent>
    </Card>
  )
}
