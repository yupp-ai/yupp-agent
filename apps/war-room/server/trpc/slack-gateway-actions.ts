import { TRPCError } from '@trpc/server'
import { z } from 'zod'
import { canCurrentUserManageAgent } from '@/lib/agents/ownership'
import {
  editAgent,
  getAgent,
  isAhsHttpError,
  listAgents,
} from '@/lib/ahs/server/client'
import {
  resolvePostSubmitSlackGatewayStatus,
  submitSlackGatewayRequest,
} from '@/lib/slack-agent-gateway/post-submit'
import {
  getSlackGatewayBotStatus,
  isSlackAgentGatewayConfigured,
  isSlackAgentGatewayHttpError,
  requestSlackGatewayBot,
} from '@/lib/slack-agent-gateway/server/client'
import {
  getEmptySlackGatewayStatus,
  hasActiveSlackGatewayRequest,
  type SlackGatewayStatus,
  slackGatewayStatusSchema,
} from '@/lib/slack-agent-gateway/types'
import { operatorAction, trpcActions } from './init'

const agentNameInputSchema = z.object({
  agentName: z.string().min(1),
})

const SLACK_GATEWAY_STATUS_RETRY_DELAYS_MS = [250, 500, 1_000, 1_500] as const

type SlackGatewayErrorMessages = {
  badRequest: string
  conflict: string
  forbidden: string
  internal: string
}

const slackGatewayStatusErrorMessages: SlackGatewayErrorMessages = {
  badRequest: 'Slack status is unavailable.',
  conflict: 'Slack status is unavailable.',
  forbidden: 'Slack status is unavailable for this agent.',
  internal: 'Slack status is unavailable.',
}

const slackGatewayInstallErrorMessages: SlackGatewayErrorMessages = {
  badRequest: 'Slack bot installation request was invalid.',
  conflict: 'A Slack bot install request is already active for this agent.',
  forbidden: 'Slack bot installation is not allowed for this agent.',
  internal: 'Slack bot installation failed.',
}

type SlackGatewayActionDependencies = {
  editAgent: typeof editAgent
  getAgent: typeof getAgent
  getSlackGatewayBotStatus: typeof getSlackGatewayBotStatus
  isSlackAgentGatewayConfigured: typeof isSlackAgentGatewayConfigured
  isAhsHttpError: typeof isAhsHttpError
  isSlackAgentGatewayHttpError: typeof isSlackAgentGatewayHttpError
  listAgents: typeof listAgents
  requestSlackGatewayBot: typeof requestSlackGatewayBot
}

const defaultSlackGatewayActionDependencies: SlackGatewayActionDependencies = {
  editAgent,
  getAgent,
  getSlackGatewayBotStatus,
  isSlackAgentGatewayConfigured,
  isAhsHttpError,
  isSlackAgentGatewayHttpError,
  listAgents,
  requestSlackGatewayBot,
}

function hasSlackGatewayEnabled(allowedGateways: readonly string[]): boolean {
  return allowedGateways.includes('*') || allowedGateways.includes('slack')
}

function getInstallPreconditionErrorMessage() {
  return 'Slack install requires the Slack gateway, and this agent is read-only in War Room.'
}

function getSlackGatewayUnavailableMessage() {
  return 'Slack bot install is unavailable because this War Room environment is missing Slack gateway configuration.'
}

async function getPostSubmitBotStatus(input: {
  agentName: string
  fallbackStatus: SlackGatewayStatus
  dependencies: SlackGatewayActionDependencies
}): Promise<SlackGatewayStatus> {
  const status = await input.dependencies.getSlackGatewayBotStatus(
    input.agentName,
    {
      notFoundRetryDelaysMs: SLACK_GATEWAY_STATUS_RETRY_DELAYS_MS,
    }
  )

  return resolvePostSubmitSlackGatewayStatus({
    fallbackStatus: input.fallbackStatus,
    polledStatus: status,
  })
}

function toTrpcError(
  error: unknown,
  dependencies: SlackGatewayActionDependencies,
  messages: SlackGatewayErrorMessages
): TRPCError {
  if (error instanceof TRPCError) {
    return error
  }

  if (dependencies.isAhsHttpError(error, 404)) {
    return new TRPCError({ code: 'NOT_FOUND', message: 'Agent not found.' })
  }

  if (
    dependencies.isAhsHttpError(error, 400) ||
    dependencies.isSlackAgentGatewayHttpError(error, 400)
  ) {
    return new TRPCError({
      code: 'BAD_REQUEST',
      message: messages.badRequest,
    })
  }

  if (
    dependencies.isAhsHttpError(error, 403) ||
    dependencies.isSlackAgentGatewayHttpError(error, 403)
  ) {
    return new TRPCError({
      code: 'FORBIDDEN',
      message: messages.forbidden,
    })
  }

  if (dependencies.isSlackAgentGatewayHttpError(error, 409)) {
    return new TRPCError({
      code: 'CONFLICT',
      message: messages.conflict,
    })
  }

  if (
    dependencies.isAhsHttpError(error) ||
    dependencies.isSlackAgentGatewayHttpError(error)
  ) {
    return new TRPCError({
      code: 'INTERNAL_SERVER_ERROR',
      message: messages.internal,
    })
  }

  if (error instanceof Error) {
    return new TRPCError({
      code: 'INTERNAL_SERVER_ERROR',
      message: messages.internal,
    })
  }

  return new TRPCError({
    code: 'INTERNAL_SERVER_ERROR',
    message: messages.internal,
  })
}

export function createSlackGatewayActions(
  dependencies: SlackGatewayActionDependencies = defaultSlackGatewayActionDependencies
) {
  return trpcActions({
    getBotStatus: operatorAction
      .input(agentNameInputSchema)
      .query(async ({ input }) => {
        if (!dependencies.isSlackAgentGatewayConfigured()) {
          return getEmptySlackGatewayStatus()
        }

        try {
          await dependencies.getAgent(input.agentName)
          const status = await dependencies.getSlackGatewayBotStatus(
            input.agentName
          )
          return slackGatewayStatusSchema.parse(status)
        } catch (error) {
          throw toTrpcError(
            error,
            dependencies,
            slackGatewayStatusErrorMessages
          )
        }
      }),

    installAgentBot: operatorAction
      .input(agentNameInputSchema)
      .mutation(async ({ ctx, input }) => {
        const requestedByEmail = ctx.session.user.email?.trim()

        if (!requestedByEmail) {
          throw new TRPCError({
            code: 'BAD_REQUEST',
            message: 'Your War Room session is missing an email address.',
          })
        }

        if (!dependencies.isSlackAgentGatewayConfigured()) {
          throw new TRPCError({
            code: 'PRECONDITION_FAILED',
            message: getSlackGatewayUnavailableMessage(),
          })
        }

        try {
          const agentDetail = await dependencies.getAgent(input.agentName)
          const currentStatus = await dependencies.getSlackGatewayBotStatus(
            input.agentName
          )

          if (
            currentStatus.hasSlackBot ||
            hasActiveSlackGatewayRequest(currentStatus)
          ) {
            return slackGatewayStatusSchema.parse(currentStatus)
          }

          const agent = agentDetail.agent

          if (!hasSlackGatewayEnabled(agent.allowed_gateways)) {
            let canManageAgent = canCurrentUserManageAgent({
              agentName: agent.name,
              creatorUserId: agent.creator_user_id,
              currentUserId: ctx.session.user.id,
            })

            if (!canManageAgent) {
              const ownedAgents = await dependencies.listAgents({
                include_all: false,
                user_id: ctx.session.user.id,
              })

              canManageAgent = canCurrentUserManageAgent({
                agentName: agent.name,
                creatorUserId: agent.creator_user_id,
                currentUserId: ctx.session.user.id,
                userAgents: ownedAgents,
              })
            }

            if (!canManageAgent) {
              throw new TRPCError({
                code: 'FORBIDDEN',
                message: getInstallPreconditionErrorMessage(),
              })
            }

            await dependencies.editAgent({
              name: agent.name,
              user_id: ctx.session.user.id,
              allowed_gateways: [
                ...new Set([...agent.allowed_gateways, 'slack']),
              ],
            })
          }

          const nextStatus = await submitSlackGatewayRequest({
            agentName: agent.name,
            displayName: agent.display_name,
            requestedAt: new Date().toISOString(),
            submitRequest: () =>
              dependencies.requestSlackGatewayBot({
                agent_name: agent.name,
                slack_name: agent.name,
                display_name: agent.display_name,
                requested_by: 'war-room',
                requested_by_email: requestedByEmail,
                description: agent.description?.trim() || undefined,
              }),
            getPostSubmitStatus: (fallbackStatus) =>
              getPostSubmitBotStatus({
                agentName: agent.name,
                fallbackStatus,
                dependencies,
              }),
            isDuplicateRequestError: (error) =>
              dependencies.isSlackAgentGatewayHttpError(error, 409),
          })
          return slackGatewayStatusSchema.parse(nextStatus)
        } catch (error) {
          throw toTrpcError(
            error,
            dependencies,
            slackGatewayInstallErrorMessages
          )
        }
      }),
  })
}

export const slackGatewayActions = createSlackGatewayActions()
