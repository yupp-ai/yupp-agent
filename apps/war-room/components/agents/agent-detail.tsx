'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useState } from 'react'
import { AgentForm } from '@/components/agents/agent-form'
import {
  AgentSlackCard,
  type AgentSlackOwnershipState,
} from '@/components/agents/agent-slack-card'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import {
  Card,
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
import { Skeleton } from '@/components/ui/skeleton'
import { canCurrentUserManageAgent } from '@/lib/agents/ownership'
import { useSession } from '@/lib/auth/session-provider'
import { useTRPC } from '@/lib/hooks/trpc-client'
import { persistSelectedAgent } from '@/lib/hooks/use-persisted-agent-selection'

type AgentDetailProps = {
  agentName: string
  isSlackGatewayAvailable: boolean
}

function JsonBlock({
  content,
  emptyState,
}: {
  content: string
  emptyState?: string
}) {
  if (!content.trim()) {
    return <p className="text-sm text-zinc-500">{emptyState ?? 'No data.'}</p>
  }

  return (
    <pre className="overflow-x-auto rounded-xl border border-zinc-800 bg-zinc-950/80 p-4 font-mono text-xs leading-6 text-zinc-300 whitespace-pre-wrap">
      {content}
    </pre>
  )
}

function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">
        {label}
      </p>
      <p className="text-sm text-zinc-200">{value}</p>
    </div>
  )
}

export function AgentDetail({
  agentName,
  isSlackGatewayAvailable,
}: AgentDetailProps) {
  const trpc = useTRPC()
  const session = useSession()
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const { data, isLoading, error } = useQuery(
    trpc.ahs.getAgent.queryOptions({
      name: agentName,
      include_system_prompts: true,
    })
  )
  const ownedAgentsQuery = useQuery(
    trpc.ahs.listAgents.queryOptions({ include_all: false })
  )

  if (isLoading) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <div className="mb-6 space-y-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-4 w-80" />
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <Skeleton className="h-64" />
          <Skeleton className="h-64" />
          <Skeleton className="h-48 lg:col-span-2" />
        </div>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <Link
          className="text-sm text-zinc-500 transition-colors hover:text-zinc-200"
          href="/agents"
        >
          ← Back to agents
        </Link>
        <p className="mt-6 text-sm text-red-400">
          {error?.message ?? 'Agent not found.'}
        </p>
      </div>
    )
  }

  const { agent, system_prompts: systemPrompts } = data
  const ownedAgents = ownedAgentsQuery.data ?? []
  const isOwner = canCurrentUserManageAgent({
    agentName: agent.name,
    creatorUserId: agent.creator_user_id,
    currentUserId: session.user.id,
    userAgents: ownedAgents,
  })
  const isCheckingOwnership = ownedAgentsQuery.isLoading && !isOwner
  const hasOwnershipError = ownedAgentsQuery.isError && !isOwner
  const isSharedGlobalAgent =
    agent.creator_user_id === null &&
    !isOwner &&
    !isCheckingOwnership &&
    !hasOwnershipError
  const slackOwnershipState: AgentSlackOwnershipState = hasOwnershipError
    ? 'error'
    : isCheckingOwnership
      ? 'checking'
      : isOwner
        ? 'owner'
        : isSharedGlobalAgent
          ? 'shared-global'
          : 'read-only'
  const promptEntries = Object.entries(systemPrompts ?? {})
  const ownerLabel = hasOwnershipError
    ? 'Ownership unavailable'
    : isCheckingOwnership
      ? 'Checking ownership...'
      : isOwner
        ? 'You'
        : isSharedGlobalAgent
          ? 'Shared/global agent'
          : 'Another operator'

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-3">
          <Link
            className="text-sm text-zinc-500 transition-colors hover:text-zinc-200"
            href="/agents"
          >
            ← Back to agents
          </Link>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-bold text-2xl tracking-tight text-zinc-50">
              {agent.display_name}
            </h1>
            <Badge>{agent.executor_type}</Badge>
            {agent.llm_model && <Badge>{agent.llm_model}</Badge>}
            {agent.executor_model && <Badge>{agent.executor_model}</Badge>}
            {isOwner && <Badge>Mine</Badge>}
            {!isCheckingOwnership &&
              !hasOwnershipError &&
              !isOwner &&
              !isSharedGlobalAgent && (
                <Badge className="bg-zinc-900 text-zinc-500">Read-only</Badge>
              )}
          </div>
          <p className="font-mono text-xs text-zinc-500">{agent.name}</p>
          <p className="max-w-3xl text-sm text-zinc-300">
            {agent.description ?? 'No description'}
          </p>
        </div>

        <div className="flex gap-2">
          {!isCheckingOwnership && !hasOwnershipError && isOwner && (
            <Button
              onClick={() => {
                setNotice(null)
                setIsEditDialogOpen(true)
              }}
              type="button"
            >
              Edit
            </Button>
          )}
          <Link
            className={buttonVariants({ variant: 'primary' })}
            href={`/?agent=${encodeURIComponent(agent.name)}`}
            onClick={() => {
              persistSelectedAgent(agent.name)
            }}
          >
            New session
          </Link>
        </div>
      </div>

      {notice && <p className="mb-4 text-sm text-emerald-400">{notice}</p>}

      <Dialog onOpenChange={setIsEditDialogOpen} open={isEditDialogOpen}>
        <DialogContent
          className="max-w-5xl overflow-hidden border border-white/15 bg-transparent p-0 shadow-none"
          showCloseButton={false}
        >
          <DialogTitle className="sr-only">
            Edit {agent.display_name}
          </DialogTitle>
          <DialogDescription className="sr-only">
            Update the editable fields for this agent.
          </DialogDescription>
          <AgentForm
            agentName={agent.name}
            mode="edit"
            onCancel={() => setIsEditDialogOpen(false)}
            onSuccess={(savedAgentName) => {
              setNotice(`Saved changes to ${savedAgentName}.`)
              setIsEditDialogOpen(false)
            }}
          />
        </DialogContent>
      </Dialog>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Runtime</CardTitle>
            <CardDescription>
              Execution and budget controls returned by AHS.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <DetailItem label="Executor type" value={agent.executor_type} />
            <DetailItem
              label="Executor model"
              value={agent.executor_model ?? '—'}
            />
            <DetailItem label="Default repo" value={agent.default_repo} />
            <DetailItem
              label="Sandbox"
              value={agent.sandbox_enabled ? 'Enabled' : 'Disabled'}
            />
            <DetailItem label="Max turns" value={String(agent.max_turns)} />
            <DetailItem label="Max budget" value={`$${agent.max_budget_usd}`} />
            <DetailItem label="Timeout" value={`${agent.timeout_s}s`} />
            <DetailItem label="Owner" value={ownerLabel} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Access</CardTitle>
            <CardDescription>
              Gateways and subagents exposed by the current AHS response.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <DetailItem
              label="Allowed gateways"
              value={
                agent.allowed_gateways.length > 0
                  ? agent.allowed_gateways.join(', ')
                  : 'None'
              }
            />
            <DetailItem
              label="Allowed subagents"
              value={
                agent.allowed_subagents.length > 0
                  ? agent.allowed_subagents.join(', ')
                  : 'None'
              }
            />
          </CardContent>
          <CardFooter className="text-xs text-zinc-500">
            AHS currently returns only the sandbox enabled flag here, not the
            full sandbox object.
          </CardFooter>
        </Card>

        <AgentSlackCard
          agentName={agent.name}
          allowedGateways={agent.allowed_gateways}
          isSlackGatewayAvailable={isSlackGatewayAvailable}
          onRefreshOwnership={async () => ownedAgentsQuery.refetch()}
          ownershipState={slackOwnershipState}
        />

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Tool Permissions</CardTitle>
            <CardDescription>Raw permission map from AHS.</CardDescription>
          </CardHeader>
          <CardContent>
            <JsonBlock
              content={JSON.stringify(agent.tool_permissions, null, 2)}
            />
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>System Prompts</CardTitle>
            <CardDescription>
              Prompt files returned by `GET /ahs/agent/{'{name}'}` with
              `include_system_prompts=true`.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {promptEntries.length === 0 ? (
              <p className="text-sm text-zinc-500">
                No prompt files were returned for this agent.
              </p>
            ) : (
              promptEntries.map(([path, content]) => (
                <div className="space-y-2" key={path}>
                  <p className="font-mono text-xs text-zinc-500">{path}</p>
                  <JsonBlock content={content} />
                </div>
              ))
            )}
            {/* TODO(yupp-mind): Extend AgentDetailResponse with additional_system_prompt and
                the remaining DB-backed config fields so the detail page can show a complete
                agent record instead of only the current summary + prompt files. */}
            <p className="text-xs text-zinc-500">
              Additional system prompt and some DB-backed config fields are not
              included in the current AHS detail response yet.
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
