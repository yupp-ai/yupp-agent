'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useTRPC } from '@/lib/hooks/trpc-client'
import {
  getSlackInstallEligibility,
  type SlackInstallOwnershipState,
} from '@/lib/slack-agent-gateway/install-state'
import { resolveVisibleSlackGatewayStatus } from '@/lib/slack-agent-gateway/post-submit'
import type { SlackGatewayStatus } from '@/lib/slack-agent-gateway/types'

const POLLING_STATUSES = new Set([
  'APPROVED',
  'AWAITING_INSTALLATION',
  'PENDING',
])

const DESCRIBED_PENDING_STATUSES = new Set([
  'APPROVED',
  'AWAITING_INSTALLATION',
  'DENIED',
  'FAILED',
  'PENDING',
])

type AgentSlackCardProps = {
  agentName: string
  allowedGateways: string[]
  isSlackGatewayAvailable: boolean
  onRefreshOwnership?: () => Promise<unknown>
  ownershipState: AgentSlackOwnershipState
}

export type AgentSlackOwnershipState = SlackInstallOwnershipState

function formatMutationError(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }

  return 'Something went wrong. Please try again.'
}

function formatDateTime(value?: string): string {
  if (!value) {
    return 'Unknown'
  }

  const timestamp = Date.parse(value)

  if (Number.isNaN(timestamp)) {
    return value
  }

  return new Date(timestamp).toLocaleString()
}

function hasSlackGatewayAccess(allowedGateways: string[]): boolean {
  return allowedGateways.includes('*') || allowedGateways.includes('slack')
}

function isPollingStatus(status?: string): boolean {
  return status ? POLLING_STATUSES.has(status) : false
}

function shouldPersistSlackStatus(status?: SlackGatewayStatus | null): boolean {
  return Boolean(
    status?.hasSlackBot || isPollingStatus(status?.pendingRequest?.status)
  )
}

function slackStatusBadgeClass(status?: string): string {
  switch (status) {
    case 'APPROVED':
      return 'bg-blue-900/40 text-blue-100'
    case 'AWAITING_INSTALLATION':
      return 'bg-sky-900/40 text-sky-100'
    case 'COMPLETED':
      return 'bg-emerald-900/40 text-emerald-100'
    case 'DENIED':
    case 'FAILED':
      return 'bg-red-900/40 text-red-100'
    case 'PENDING':
      return 'bg-amber-900/40 text-amber-100'
    default:
      return 'bg-zinc-800 text-zinc-300'
  }
}

function SlackDetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">
        {label}
      </p>
      <p className="font-mono text-xs text-zinc-200">{value}</p>
    </div>
  )
}

export function AgentSlackCard({
  agentName,
  allowedGateways,
  isSlackGatewayAvailable,
  onRefreshOwnership,
  ownershipState,
}: AgentSlackCardProps) {
  const trpc = useTRPC()
  const queryClient = useQueryClient()
  const [actionError, setActionError] = useState<string | null>(null)
  const [lastKnownStatus, setLastKnownStatus] = useState<{
    agentName: string
    status: SlackGatewayStatus
  } | null>(null)
  const slackGatewayEnabled = hasSlackGatewayAccess(allowedGateways)
  const botStatusQueryOptions = trpc.slackGateway.getBotStatus.queryOptions({
    agentName,
  })
  const fallbackStatus =
    lastKnownStatus?.agentName === agentName ? lastKnownStatus.status : null

  const statusQuery = useQuery({
    ...botStatusQueryOptions,
    enabled: isSlackGatewayAvailable,
    refetchInterval(query) {
      const pendingStatus =
        query.state.data?.pendingRequest?.status ??
        fallbackStatus?.pendingRequest?.status

      return isPollingStatus(pendingStatus) ? 5_000 : false
    },
  })

  useEffect(() => {
    setLastKnownStatus((previousStatus) => {
      const queryStatus = statusQuery.data
      const priorVisibleStatus =
        previousStatus?.agentName === agentName ? previousStatus.status : null

      if (queryStatus && shouldPersistSlackStatus(queryStatus)) {
        return {
          agentName,
          status:
            resolveVisibleSlackGatewayStatus({
              currentStatus: queryStatus,
              lastKnownStatus: priorVisibleStatus,
            }) ?? queryStatus,
        }
      }

      if (queryStatus?.hasSlackBot || queryStatus?.pendingRequest) {
        return null
      }

      return previousStatus?.agentName === agentName ? previousStatus : null
    })
  }, [agentName, statusQuery.data])

  async function refreshSlackInstallState() {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: botStatusQueryOptions.queryKey,
      }),
      queryClient.invalidateQueries(trpc.ahs.getAgent.pathFilter()),
      queryClient.invalidateQueries(trpc.ahs.listAgents.pathFilter()),
    ])
  }

  const installMutation = useMutation(
    trpc.slackGateway.installAgentBot.mutationOptions({
      async onSuccess(status) {
        setActionError(null)
        if (shouldPersistSlackStatus(status)) {
          setLastKnownStatus((previousStatus) => ({
            agentName,
            status:
              resolveVisibleSlackGatewayStatus({
                currentStatus: status,
                lastKnownStatus:
                  previousStatus?.agentName === agentName
                    ? previousStatus.status
                    : null,
              }) ?? status,
          }))
        } else {
          setLastKnownStatus(null)
        }
        queryClient.setQueryData(botStatusQueryOptions.queryKey, status)
      },
      onError(error) {
        setActionError(formatMutationError(error))
      },
      async onSettled() {
        await refreshSlackInstallState()
      },
    })
  )

  const status = resolveVisibleSlackGatewayStatus({
    currentStatus: statusQuery.data,
    lastKnownStatus: fallbackStatus,
  })
  const isOwner = ownershipState === 'owner'
  const isCheckingOwnership = ownershipState === 'checking'
  const hasOwnershipError = ownershipState === 'error'

  function handleInstall() {
    setActionError(null)
    installMutation.mutate({ agentName })
  }

  async function handleRefresh() {
    const refreshes: Promise<unknown>[] = [statusQuery.refetch()]

    if (shouldRefreshOwnership && onRefreshOwnership) {
      refreshes.push(onRefreshOwnership())
    }

    await Promise.all(refreshes)
  }

  if (!isSlackGatewayAvailable) {
    return (
      <Card className="lg:col-span-2">
        <CardHeader>
          <CardAction>
            <Badge className="bg-zinc-900 text-zinc-400">Unavailable</Badge>
          </CardAction>
          <CardTitle>Slack</CardTitle>
          <CardDescription>
            Install and monitor the Slack gateway bot for this agent.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-zinc-300">
            Slack bot install is unavailable in this War Room environment.
          </p>
          <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
            Set <span className="font-mono">SLACK_AGENT_GATEWAY_HOST</span> and{' '}
            <span className="font-mono">SLACK_AGENT_GATEWAY_API_KEY</span> to
            enable the Slack install flow.
          </div>
        </CardContent>
      </Card>
    )
  }

  if (statusQuery.isLoading && !status) {
    return (
      <Card className="lg:col-span-2">
        <CardHeader>
          <Skeleton className="h-6 w-24" />
          <Skeleton className="h-4 w-80" />
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-20 sm:col-span-2" />
        </CardContent>
      </Card>
    )
  }

  if (statusQuery.error && !status) {
    return (
      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle>Slack</CardTitle>
          <CardDescription>
            War Room could not load the Slack bot status for this agent.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-red-400">
            {statusQuery.error?.message ?? 'Slack status is unavailable.'}
          </p>
        </CardContent>
        <CardFooter className="justify-end gap-2">
          <Button onClick={() => void statusQuery.refetch()} type="button">
            Retry
          </Button>
        </CardFooter>
      </Card>
    )
  }

  if (!status) {
    return null
  }

  const { hasSlackBot, pendingRequest, slackAppId, slackName } = status
  const requestStatus = pendingRequest?.status
  const isBlockedByGateway =
    !hasSlackBot &&
    !pendingRequest &&
    !slackGatewayEnabled &&
    !isOwner &&
    !hasOwnershipError &&
    !isCheckingOwnership
  const isWaitingForOwnership =
    !hasSlackBot &&
    !pendingRequest &&
    !slackGatewayEnabled &&
    isCheckingOwnership
  const isBlockedByOwnershipError =
    !hasSlackBot && !pendingRequest && !slackGatewayEnabled && hasOwnershipError
  const isSharedGlobalAgent = ownershipState === 'shared-global'
  const isAwaitingInstallationWithoutUrl =
    pendingRequest?.status === 'AWAITING_INSTALLATION' &&
    !pendingRequest.oauthInstallUrl
  const {
    canInstall,
    canRetryInstall,
    isRetryBlockedByGateway,
    isRetryBlockedByOwnershipError,
    isRetryWaitingForOwnership,
    shouldRefreshOwnership,
  } = getSlackInstallEligibility({
    hasSlackBot,
    isSlackGatewayEnabled: slackGatewayEnabled,
    ownershipState,
    requestStatus,
  })

  let badgeLabel = 'Not installed'

  if (hasSlackBot) {
    badgeLabel = 'Live'
  } else if (isAwaitingInstallationWithoutUrl) {
    badgeLabel = 'Blocked'
  } else if (isBlockedByOwnershipError) {
    badgeLabel = 'Retry'
  } else if (requestStatus) {
    badgeLabel = requestStatus
  } else if (isBlockedByGateway) {
    badgeLabel = 'Blocked'
  }

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardAction className="flex items-center gap-2">
          <Badge
            className={slackStatusBadgeClass(
              hasSlackBot
                ? 'COMPLETED'
                : isAwaitingInstallationWithoutUrl
                  ? 'FAILED'
                  : isBlockedByOwnershipError
                    ? 'FAILED'
                    : requestStatus
            )}
          >
            {badgeLabel}
          </Badge>
          {isPollingStatus(requestStatus) && (
            <Badge className="bg-zinc-900 text-zinc-400">Polling 5s</Badge>
          )}
        </CardAction>
        <CardTitle>Slack</CardTitle>
        <CardDescription>
          Install and monitor the Slack gateway bot for this agent.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {hasSlackBot ? (
          <>
            <p className="text-sm text-zinc-300">
              This agent already has a live Slack bot connected through the
              gateway.
            </p>
            <div className="grid gap-4 sm:grid-cols-2">
              <SlackDetailItem
                label="Slack name"
                value={slackName ?? 'Unavailable'}
              />
              <SlackDetailItem
                label="Slack app ID"
                value={slackAppId ?? 'Unavailable'}
              />
            </div>
          </>
        ) : pendingRequest ? (
          <>
            <p className="text-sm text-zinc-300">
              {requestStatus === 'PENDING' &&
                'The request has been submitted. Slack admins still need to approve app creation.'}
              {requestStatus === 'APPROVED' &&
                'The request is approved. The Slack app is being provisioned now.'}
              {requestStatus === 'AWAITING_INSTALLATION' &&
                !isAwaitingInstallationWithoutUrl &&
                'The Slack app is ready. Finish the OAuth step to complete installation.'}
              {isAwaitingInstallationWithoutUrl &&
                'The gateway reports AWAITING_INSTALLATION, but it has not returned an install URL yet. Refresh to check again, and contact a Slack admin if this stays blocked.'}
              {requestStatus === 'DENIED' &&
                (isRetryBlockedByGateway
                  ? 'Slack admins denied this request. War Room cannot retry until this agent allows the Slack gateway again.'
                  : isRetryWaitingForOwnership
                    ? 'Slack admins denied this request. War Room is checking whether it can re-enable the Slack gateway before retrying.'
                    : isRetryBlockedByOwnershipError
                      ? 'Slack admins denied this request. Refresh ownership before retrying so War Room can confirm whether it may re-enable the Slack gateway.'
                      : 'Slack admins denied this request. Review the reason below, then retry if appropriate.')}
              {requestStatus === 'FAILED' &&
                (isRetryBlockedByGateway
                  ? 'Slack app setup failed, and War Room cannot retry until this agent allows the Slack gateway again.'
                  : isRetryWaitingForOwnership
                    ? 'Slack app setup failed. War Room is checking whether it can re-enable the Slack gateway before retrying.'
                    : isRetryBlockedByOwnershipError
                      ? 'Slack app setup failed. Refresh ownership before retrying so War Room can confirm whether it may re-enable the Slack gateway.'
                      : 'Slack app setup failed after approval. Review the latest error before retrying.')}
              {!DESCRIBED_PENDING_STATUSES.has(requestStatus ?? '') &&
                'War Room is tracking an active Slack bot request for this agent.'}
            </p>
            <div className="grid gap-4 sm:grid-cols-2">
              <SlackDetailItem
                label="Request ID"
                value={pendingRequest.requestId}
              />
              <SlackDetailItem
                label="Display name"
                value={pendingRequest.displayName}
              />
              <SlackDetailItem
                label="Created"
                value={formatDateTime(pendingRequest.createdAt)}
              />
              <SlackDetailItem
                label="Updated"
                value={formatDateTime(pendingRequest.updatedAt)}
              />
            </div>
            {pendingRequest.error && (
              <div className="rounded-xl border border-red-900/60 bg-red-950/30 p-3 text-sm text-red-200">
                {pendingRequest.error}
              </div>
            )}
            {isAwaitingInstallationWithoutUrl && (
              <div className="rounded-xl border border-amber-900/60 bg-amber-950/30 p-3 text-sm text-amber-100">
                War Room cannot continue the OAuth install until the Slack
                gateway returns a valid install link for this request.
              </div>
            )}
            {isRetryBlockedByGateway && (
              <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
                {isSharedGlobalAgent ? (
                  <>
                    Update the agent in AHS to add{' '}
                    <span className="font-mono">slack</span> or{' '}
                    <span className="font-mono">*</span> to the allowed gateways
                    before retrying from War Room.
                  </>
                ) : (
                  <>
                    Ask the agent owner to add{' '}
                    <span className="font-mono">slack</span> or{' '}
                    <span className="font-mono">*</span> to the allowed gateways
                    before retrying from War Room.
                  </>
                )}
              </div>
            )}
          </>
        ) : isBlockedByOwnershipError ? (
          <>
            <p className="text-sm text-zinc-300">
              War Room could not verify whether you own this agent, so it cannot
              auto-enable the Slack gateway yet.
            </p>
            <div className="rounded-xl border border-red-900/60 bg-red-950/30 p-3 text-sm text-red-200">
              Refresh ownership and try again. If this keeps failing, check the
              AHS agents list request for this operator.
            </div>
          </>
        ) : isWaitingForOwnership ? (
          <p className="text-sm text-zinc-300">
            War Room is checking whether you own this agent before auto-enabling
            the Slack gateway.
          </p>
        ) : isBlockedByGateway ? (
          <>
            <p className="text-sm text-zinc-300">
              {isSharedGlobalAgent
                ? 'This shared or global agent does not allow the Slack gateway yet, and War Room cannot auto-enable it.'
                : 'This agent does not allow the Slack gateway yet, and only the owner can enable it.'}
            </p>
            <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3 text-sm text-zinc-400">
              {isSharedGlobalAgent ? (
                <>
                  Update the agent in AHS to add{' '}
                  <span className="font-mono">slack</span> or{' '}
                  <span className="font-mono">*</span> to the allowed gateways,
                  then retry from War Room.
                </>
              ) : (
                <>
                  Ask the agent owner to add{' '}
                  <span className="font-mono">slack</span> or{' '}
                  <span className="font-mono">*</span> to the allowed gateways,
                  then retry from War Room.
                </>
              )}
            </div>
          </>
        ) : (
          <>
            <p className="text-sm text-zinc-300">
              Kick off Slack bot setup from War Room. The request is derived
              from the current agent configuration.
            </p>
            {!slackGatewayEnabled && isOwner && (
              <div className="rounded-xl border border-blue-900/60 bg-blue-950/20 p-3 text-sm text-blue-100">
                Slack is not enabled on this agent yet. War Room will add the
                gateway automatically before submitting the install request.
              </div>
            )}
          </>
        )}

        {actionError && (
          <div className="rounded-xl border border-red-900/60 bg-red-950/30 p-3 text-sm text-red-200">
            {actionError}
          </div>
        )}
      </CardContent>
      <CardFooter className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-zinc-500">
          {hasSlackBot && 'Status synced from the Slack gateway service.'}
          {isAwaitingInstallationWithoutUrl &&
            'Install link missing. Keep refreshing while the gateway syncs, or escalate to a Slack admin if it stays unavailable.'}
          {isRetryBlockedByGateway &&
            (isSharedGlobalAgent
              ? 'Retry is blocked until this shared or global agent allows the Slack gateway again.'
              : 'Retry is blocked until the agent owner enables the Slack gateway again.')}
          {isRetryBlockedByOwnershipError &&
            'Ownership lookup failed. Refresh before War Room can determine whether it may retry.'}
          {isRetryWaitingForOwnership &&
            'Ownership must resolve before War Room can determine whether it may retry.'}
          {!hasSlackBot &&
            pendingRequest &&
            !isRetryBlockedByGateway &&
            !isRetryBlockedByOwnershipError &&
            !isRetryWaitingForOwnership &&
            !isAwaitingInstallationWithoutUrl &&
            `Last updated ${formatDateTime(pendingRequest.updatedAt)}`}
          {isBlockedByOwnershipError &&
            'Ownership lookup failed. Retry before War Room can auto-enable Slack for this agent.'}
          {isWaitingForOwnership &&
            'Ownership must resolve before War Room can auto-enable Slack.'}
          {isBlockedByGateway &&
            (isSharedGlobalAgent
              ? 'Shared and global agents need their allowed gateways updated outside War Room first.'
              : 'Read-only agents need their owner to enable the Slack gateway first.')}
          {!hasSlackBot &&
            !pendingRequest &&
            !isBlockedByOwnershipError &&
            !isWaitingForOwnership &&
            !isBlockedByGateway &&
            'War Room will use the current agent record and your operator session to start setup.'}
        </div>
        <div className="flex flex-wrap gap-2">
          {pendingRequest?.status === 'AWAITING_INSTALLATION' &&
            pendingRequest.oauthInstallUrl && (
              <a
                className={buttonVariants({ variant: 'primary' })}
                href={pendingRequest.oauthInstallUrl}
                rel="noopener noreferrer"
                target="_blank"
              >
                Continue Install
              </a>
            )}
          {(canRetryInstall || canInstall) && (
            <Button
              disabled={installMutation.isPending}
              onClick={handleInstall}
              type="button"
              variant="primary"
            >
              {installMutation.isPending
                ? 'Submitting...'
                : pendingRequest
                  ? 'Retry Install'
                  : 'Install Slack Bot'}
            </Button>
          )}
          {(isPollingStatus(requestStatus) || shouldRefreshOwnership) && (
            <Button
              disabled={statusQuery.isFetching}
              onClick={() => void handleRefresh()}
              type="button"
            >
              Refresh
            </Button>
          )}
        </div>
      </CardFooter>
    </Card>
  )
}
