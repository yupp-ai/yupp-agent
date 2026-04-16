'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useState } from 'react'
import { AgentForm } from '@/components/agents/agent-form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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

const loadingCardKeys = [
  'agent-skeleton-1',
  'agent-skeleton-2',
  'agent-skeleton-3',
  'agent-skeleton-4',
  'agent-skeleton-5',
  'agent-skeleton-6',
]

export default function AgentsPage() {
  const trpc = useTRPC()
  const session = useSession()
  const [isCreateFormOpen, setIsCreateFormOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const {
    data: agents = [],
    isLoading,
    error,
  } = useQuery(trpc.ahs.listAgents.queryOptions())
  const userAgentsQuery = useQuery(
    trpc.ahs.listAgents.queryOptions({ include_all: false })
  )
  const userAgents = userAgentsQuery.data ?? []

  function isOwnedAgent(agent: (typeof agents)[number]) {
    return canCurrentUserManageAgent({
      agentName: agent.name,
      creatorUserId: agent.creator_user_id,
      currentUserId: session.user.id,
      userAgents,
    })
  }

  const sortedAgents = agents.toSorted((leftAgent, rightAgent) => {
    const leftIsOwner = isOwnedAgent(leftAgent)
    const rightIsOwner = isOwnedAgent(rightAgent)

    if (leftIsOwner === rightIsOwner) {
      return 0
    }

    return leftIsOwner ? -1 : 1
  })

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-bold text-lg tracking-tight">Agents</h1>
          <p className="mt-1 text-sm text-zinc-500">
            Create agents, then click into one to inspect its config and edit
            the agents you own.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => {
              setNotice(null)
              setIsCreateFormOpen(true)
            }}
            type="button"
            variant="primary"
          >
            New agent
          </Button>
        </div>
      </div>

      {notice && <p className="mb-4 text-sm text-emerald-400">{notice}</p>}

      <Dialog onOpenChange={setIsCreateFormOpen} open={isCreateFormOpen}>
        <DialogContent
          className="max-w-5xl overflow-hidden border border-white/15 bg-transparent p-0 shadow-none"
          showCloseButton={false}
        >
          <DialogTitle className="sr-only">New agent</DialogTitle>
          <DialogDescription className="sr-only">
            Create a War Room agent backed by the AHS agent APIs.
          </DialogDescription>
          <AgentForm
            mode="create"
            onCancel={() => setIsCreateFormOpen(false)}
            onSuccess={(savedAgentName) => {
              setNotice(`Created ${savedAgentName}.`)
              setIsCreateFormOpen(false)
            }}
          />
        </DialogContent>
      </Dialog>

      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {loadingCardKeys.map((cardKey) => (
            <Card key={cardKey} size="sm">
              <CardHeader>
                <Skeleton className="h-5 w-32" />
                <Skeleton className="h-4 w-48" />
              </CardHeader>
              <CardContent>
                <div className="flex gap-2">
                  <Skeleton className="h-5 w-16 rounded-full" />
                  <Skeleton className="h-5 w-20 rounded-full" />
                </div>
              </CardContent>
              <CardFooter>
                <div className="flex gap-3">
                  <Skeleton className="h-3 w-16" />
                  <Skeleton className="h-3 w-16" />
                  <Skeleton className="h-3 w-16" />
                </div>
              </CardFooter>
            </Card>
          ))}
        </div>
      )}

      {error && <p className="text-sm text-red-400">{error.message}</p>}

      {!isLoading && !error && agents.length === 0 && (
        <p className="text-sm text-zinc-400">
          No agents found. Make sure the AHS backend is running and configured.
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {sortedAgents.map((agent) => {
          const isOwner = isOwnedAgent(agent)
          const isCheckingOwnership = userAgentsQuery.isLoading && !isOwner
          const hasOwnershipError = userAgentsQuery.isError && !isOwner
          const isSharedGlobalAgent =
            agent.creator_user_id === null &&
            !isOwner &&
            !isCheckingOwnership &&
            !hasOwnershipError

          return (
            <Link
              className="block h-full"
              href={`/agents/${encodeURIComponent(agent.name)}`}
              key={agent.name}
            >
              <Card
                className="h-full transition-colors hover:bg-zinc-900/80 hover:ring-foreground/20"
                size="sm"
              >
                <CardHeader>
                  <CardTitle>{agent.display_name}</CardTitle>
                  <CardDescription>{agent.description}</CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="flex flex-wrap gap-2">
                    <Badge>{agent.executor_type}</Badge>
                    {agent.llm_model && <Badge>{agent.llm_model}</Badge>}
                    {agent.executor_model && (
                      <Badge>{agent.executor_model}</Badge>
                    )}
                    {isOwner && <Badge>Mine</Badge>}
                    {!isCheckingOwnership &&
                      !hasOwnershipError &&
                      !isOwner &&
                      !isSharedGlobalAgent && (
                        <Badge className="bg-zinc-900 text-zinc-500">
                          Read-only
                        </Badge>
                      )}
                  </div>
                </CardContent>
                <CardFooter>
                  <div className="flex flex-wrap gap-3 text-xs text-zinc-500">
                    <span>max {agent.max_turns} turns</span>
                    <span>budget ${agent.max_budget_usd}</span>
                    <span>timeout {agent.timeout_s}s</span>
                  </div>
                </CardFooter>
              </Card>
            </Link>
          )
        })}
      </div>
    </div>
  )
}
