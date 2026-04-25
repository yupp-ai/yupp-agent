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
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import type { AhsAgentDetailResponse } from '@/lib/ahs/types'
import {
  parseRequiredJsonRecord,
  splitCommaSeparatedValues,
  stringifyJsonValue,
  trimToUndefined,
} from '@/lib/form-utils'
import { useTRPC } from '@/lib/hooks/trpc-client'

const AGENT_NAME_PATTERN = /^[a-z0-9][a-z0-9-]*$/

const EXECUTOR_OPTIONS = [
  { label: 'Harnessed', value: 'harnessed' },
  { label: 'Raw', value: 'raw' },
]

type AgentFormMode = 'create' | 'edit'

type AgentFormProps = {
  mode: AgentFormMode
  agentName?: string
  onCancel: () => void
  onSuccess: (agentName: string) => void
}

type AgentFormState = {
  name: string
  displayName: string
  description: string
  executorType: string
  executorModel: string
  toolPermissions: string
  allowedSubagents: string
  defaultRepo: string
  sandboxEnabled: boolean
  maxTurns: string
  maxBudgetUsd: string
  timeoutSeconds: string
  allowedGateways: string
  roleMd: string
  soulMd: string
  additionalSystemPrompt: string
}

function createDefaultFormState(): AgentFormState {
  return {
    name: '',
    displayName: '',
    description: '',
    executorType: 'harnessed',
    executorModel: '',
    toolPermissions: stringifyJsonValue({ '*': 'allow' }),
    allowedSubagents: '',
    defaultRepo: 'yupp-agent',
    sandboxEnabled: true,
    maxTurns: '20',
    maxBudgetUsd: '2',
    timeoutSeconds: '300',
    allowedGateways: '*',
    roleMd: '',
    soulMd: '',
    additionalSystemPrompt: '',
  }
}

function getAgentPrompt(
  detail: AhsAgentDetailResponse,
  fileName: 'ROLE.md' | 'SOUL.md'
): string {
  return detail.system_prompts?.[`${detail.agent.name}/${fileName}`] ?? ''
}

function createFormStateFromDetail(
  detail: AhsAgentDetailResponse
): AgentFormState {
  return {
    name: detail.agent.name,
    displayName: detail.agent.display_name,
    description: detail.agent.description ?? '',
    executorType: detail.agent.executor_type.toLowerCase(),
    executorModel: detail.agent.executor_model ?? detail.agent.llm_model ?? '',
    toolPermissions: stringifyJsonValue(detail.agent.tool_permissions),
    allowedSubagents: detail.agent.allowed_subagents.join(', '),
    defaultRepo: detail.agent.default_repo,
    sandboxEnabled: detail.agent.sandbox_enabled,
    maxTurns: String(detail.agent.max_turns),
    maxBudgetUsd: String(detail.agent.max_budget_usd),
    timeoutSeconds: String(detail.agent.timeout_s),
    allowedGateways: detail.agent.allowed_gateways.join(', '),
    roleMd: getAgentPrompt(detail, 'ROLE.md'),
    soulMd: getAgentPrompt(detail, 'SOUL.md'),
    additionalSystemPrompt: '',
  }
}

function formatMutationError(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }

  return 'Something went wrong. Please try again.'
}

function parsePositiveNumber(
  rawValue: string,
  fieldName: string
): { error?: string; value?: number } {
  const trimmedValue = rawValue.trim()

  if (trimmedValue.length === 0) {
    return { error: `${fieldName} is required.` }
  }

  const value = Number(trimmedValue)

  if (!Number.isFinite(value) || value <= 0) {
    return { error: `${fieldName} must be greater than 0.` }
  }

  return { value }
}

export function AgentForm({
  mode,
  agentName,
  onCancel,
  onSuccess,
}: AgentFormProps) {
  const trpc = useTRPC()
  const queryClient = useQueryClient()
  const [formState, setFormState] = useState<AgentFormState>(() =>
    createDefaultFormState()
  )
  const [formError, setFormError] = useState<string | null>(null)

  const detailQuery = useQuery({
    ...trpc.ahs.getAgent.queryOptions({
      name: agentName ?? '',
      include_system_prompts: true,
    }),
    enabled: mode === 'edit' && !!agentName,
  })

  useEffect(() => {
    if (mode !== 'edit' || !detailQuery.data) {
      return
    }

    setFormState(createFormStateFromDetail(detailQuery.data))
  }, [detailQuery.data, mode])

  const createAgentMutation = useMutation(
    trpc.ahs.createAgent.mutationOptions({
      async onSuccess(result) {
        await queryClient.invalidateQueries(trpc.ahs.listAgents.pathFilter())
        onSuccess(result.name)
      },
      onError(error) {
        setFormError(formatMutationError(error))
      },
    })
  )

  const editAgentMutation = useMutation(
    trpc.ahs.editAgent.mutationOptions({
      async onSuccess(result) {
        await Promise.all([
          queryClient.invalidateQueries(trpc.ahs.listAgents.pathFilter()),
          queryClient.invalidateQueries(trpc.ahs.getAgent.pathFilter()),
        ])
        onSuccess(result.name)
      },
      onError(error) {
        setFormError(formatMutationError(error))
      },
    })
  )

  const isPending = createAgentMutation.isPending || editAgentMutation.isPending
  const agent = detailQuery.data?.agent

  const title =
    mode === 'create'
      ? 'New agent'
      : `Edit ${agent?.display_name ?? agentName ?? 'agent'}`

  const description =
    mode === 'create'
      ? 'Create a War Room agent backed by the AHS agent APIs.'
      : 'Update an agent you own. Global agents remain read-only in AHS.'

  const additionalSystemPromptHint = useMemo(
    () =>
      mode === 'edit'
        ? 'Leave blank to preserve the current value. War Room cannot prefill this yet.'
        : 'Optional extra instructions appended after the standard agent prompts.',
    [mode]
  )

  function resetForm() {
    if (mode === 'create') {
      setFormState(createDefaultFormState())
      setFormError(null)
      return
    }

    if (detailQuery.data) {
      setFormState(createFormStateFromDetail(detailQuery.data))
      setFormError(null)
    }
  }

  function updateField<Key extends keyof AgentFormState>(
    key: Key,
    value: AgentFormState[Key]
  ) {
    setFormState((current) => ({ ...current, [key]: value }))
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)

    if (mode === 'edit' && !agent) {
      setFormError('Agent details must load before saving changes.')
      return
    }

    if (mode === 'create' && !AGENT_NAME_PATTERN.test(formState.name.trim())) {
      setFormError(
        'Agent name must match [a-z0-9-]+ and start with a letter or number.'
      )
      return
    }

    const toolPermissions = parseRequiredJsonRecord(
      formState.toolPermissions,
      'Tool permissions'
    )

    if (toolPermissions.error) {
      setFormError(toolPermissions.error)
      return
    }

    const maxTurns = parsePositiveNumber(formState.maxTurns, 'Max turns')
    const maxBudgetUsd = parsePositiveNumber(
      formState.maxBudgetUsd,
      'Max budget'
    )
    const timeoutSeconds = parsePositiveNumber(
      formState.timeoutSeconds,
      'Timeout'
    )

    const numericError =
      maxTurns.error ?? maxBudgetUsd.error ?? timeoutSeconds.error
    if (numericError) {
      setFormError(numericError)
      return
    }

    const payload = {
      display_name: trimToUndefined(formState.displayName),
      description: trimToUndefined(formState.description),
      executor_config: {
        type: formState.executorType,
        model: trimToUndefined(formState.executorModel),
      },
      tool_permissions: toolPermissions.value,
      allowed_subagents: splitCommaSeparatedValues(formState.allowedSubagents),
      default_repo: trimToUndefined(formState.defaultRepo),
      sandbox: {
        enabled: formState.sandboxEnabled,
      },
      max_turns: maxTurns.value,
      max_budget_usd: maxBudgetUsd.value,
      timeout_s: timeoutSeconds.value,
      allowed_gateways: splitCommaSeparatedValues(formState.allowedGateways),
      role_md: trimToUndefined(formState.roleMd),
      soul_md: trimToUndefined(formState.soulMd),
    }

    const additionalSystemPrompt = trimToUndefined(
      formState.additionalSystemPrompt
    )

    if (mode === 'create') {
      createAgentMutation.mutate({
        name: formState.name.trim(),
        ...payload,
        ...(additionalSystemPrompt
          ? { additional_system_prompt: additionalSystemPrompt }
          : {}),
      })
      return
    }

    if (!agentName) {
      setFormError('No agent selected for editing.')
      return
    }

    editAgentMutation.mutate({
      name: agentName,
      ...payload,
      ...(additionalSystemPrompt
        ? { additional_system_prompt: additionalSystemPrompt }
        : {}),
    })
  }

  if (mode === 'edit' && detailQuery.isLoading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-40" />
          <Skeleton className="h-4 w-80" />
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          {['1', '2', '3', '4'].map((key) => (
            <Skeleton className="h-16" key={key} />
          ))}
          <Skeleton className="h-40 md:col-span-2" />
          <Skeleton className="h-40 md:col-span-2" />
        </CardContent>
      </Card>
    )
  }

  if (mode === 'edit' && detailQuery.error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Unable to load agent</CardTitle>
          <CardDescription>
            The agent details could not be loaded for editing.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-red-400">{detailQuery.error.message}</p>
        </CardContent>
        <CardFooter className="justify-end gap-2">
          <Button onClick={() => void detailQuery.refetch()} type="button">
            Retry
          </Button>
          <Button onClick={onCancel} type="button">
            Close
          </Button>
        </CardFooter>
      </Card>
    )
  }

  if (mode === 'edit' && !agent) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Agent not found</CardTitle>
          <CardDescription>
            War Room could not find the selected agent.
          </CardDescription>
        </CardHeader>
        <CardFooter className="justify-end">
          <Button onClick={onCancel} type="button">
            Close
          </Button>
        </CardFooter>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <form className="space-y-6" onSubmit={handleSubmit}>
          <div className="grid gap-4 md:grid-cols-2">
            <Label>
              Agent name
              <Input
                disabled={mode === 'edit'}
                onChange={(event) => updateField('name', event.target.value)}
                placeholder="ops-agent"
                value={formState.name}
              />
            </Label>

            <Label>
              Display name
              <Input
                onChange={(event) =>
                  updateField('displayName', event.target.value)
                }
                placeholder="Ops Agent"
                value={formState.displayName}
              />
            </Label>

            <Label className="md:col-span-2">
              Description
              <Textarea
                className="min-h-24"
                onChange={(event) =>
                  updateField('description', event.target.value)
                }
                placeholder="What this agent is for."
                value={formState.description}
              />
            </Label>

            <Label>
              Executor type
              <Select
                onValueChange={(value) => updateField('executorType', value)}
                options={EXECUTOR_OPTIONS}
                value={formState.executorType}
              />
            </Label>

            <Label>
              Executor model
              <Input
                onChange={(event) =>
                  updateField('executorModel', event.target.value)
                }
                placeholder="anthropic/claude-sonnet-4-6"
                value={formState.executorModel}
              />
            </Label>

            <Label>
              Default repo
              <Input
                onChange={(event) =>
                  updateField('defaultRepo', event.target.value)
                }
                value={formState.defaultRepo}
              />
            </Label>

            <Label className="flex-row items-center gap-3 self-end text-sm text-zinc-300">
              <input
                checked={formState.sandboxEnabled}
                className="size-4 rounded border border-zinc-700 bg-zinc-900"
                onChange={(event) =>
                  updateField('sandboxEnabled', event.target.checked)
                }
                type="checkbox"
              />
              Sandbox enabled
            </Label>

            <Label>
              Max turns
              <Input
                min="1"
                onChange={(event) =>
                  updateField('maxTurns', event.target.value)
                }
                type="number"
                value={formState.maxTurns}
              />
            </Label>

            <Label>
              Max budget (USD)
              <Input
                min="0.01"
                onChange={(event) =>
                  updateField('maxBudgetUsd', event.target.value)
                }
                step="0.01"
                type="number"
                value={formState.maxBudgetUsd}
              />
            </Label>

            <Label>
              Timeout (seconds)
              <Input
                min="1"
                onChange={(event) =>
                  updateField('timeoutSeconds', event.target.value)
                }
                type="number"
                value={formState.timeoutSeconds}
              />
            </Label>

            <Label>
              Allowed gateways
              <Input
                onChange={(event) =>
                  updateField('allowedGateways', event.target.value)
                }
                placeholder="*, slack"
                value={formState.allowedGateways}
              />
            </Label>

            <Label>
              Allowed subagents
              <Input
                onChange={(event) =>
                  updateField('allowedSubagents', event.target.value)
                }
                placeholder="reviewer, release-bot"
                value={formState.allowedSubagents}
              />
            </Label>
          </div>

          <Label>
            Tool permissions
            <Textarea
              className="min-h-44 font-mono text-xs"
              onChange={(event) =>
                updateField('toolPermissions', event.target.value)
              }
              value={formState.toolPermissions}
            />
          </Label>

          <div className="grid gap-4 lg:grid-cols-2">
            <Label>
              ROLE.md
              <Textarea
                className="min-h-56"
                onChange={(event) => updateField('roleMd', event.target.value)}
                value={formState.roleMd}
              />
            </Label>

            <Label>
              SOUL.md
              <Textarea
                className="min-h-56"
                onChange={(event) => updateField('soulMd', event.target.value)}
                value={formState.soulMd}
              />
            </Label>
          </div>

          <Label>
            Additional system prompt
            <Textarea
              className="min-h-32"
              onChange={(event) =>
                updateField('additionalSystemPrompt', event.target.value)
              }
              placeholder="Optional extra system prompt"
              value={formState.additionalSystemPrompt}
            />
            {/* TODO: Extend GET /ahs/agent/{name} to return additional_system_prompt,
                feedback_probability, feedback_min_turns, and the full sandbox config so War Room
                can render a lossless edit form instead of treating these fields as write-only. */}
            <p className="text-xs text-zinc-500">
              {additionalSystemPromptHint}
            </p>
          </Label>

          {formError && (
            <p className="mt-4 text-sm text-red-400">{formError}</p>
          )}

          <CardFooter className="justify-end gap-2 px-0">
            <Button
              onClick={() => {
                resetForm()
                onCancel()
              }}
              type="button"
            >
              Cancel
            </Button>
            <Button disabled={isPending} type="submit" variant="primary">
              {isPending
                ? mode === 'create'
                  ? 'Creating...'
                  : 'Saving...'
                : mode === 'create'
                  ? 'New agent'
                  : 'Save changes'}
            </Button>
          </CardFooter>
        </form>
      </CardContent>
    </Card>
  )
}
