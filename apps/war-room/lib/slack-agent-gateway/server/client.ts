import 'server-only'

import type { z } from 'zod'
import {
  type BotCreationRequest,
  botCreationRequestSchema,
  getEmptySlackGatewayStatus,
  type SlackGatewayBotCreationResponse,
  type SlackGatewayBotStatusResponse,
  type SlackGatewayStatus,
  sanitizeSlackGatewayStatus,
  slackGatewayBotCreationResponseSchema,
  slackGatewayBotStatusResponseSchema,
} from '../types'

export class SlackAgentGatewayHttpError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'SlackAgentGatewayHttpError'
    this.status = status
  }
}

export function isSlackAgentGatewayHttpError(
  error: unknown,
  status?: number
): error is SlackAgentGatewayHttpError {
  return (
    error instanceof SlackAgentGatewayHttpError &&
    (status === undefined || error.status === status)
  )
}

type GetSlackGatewayBotStatusOptions = {
  notFoundRetryDelaysMs?: readonly number[]
  treat404AsEmpty?: boolean
}

const SLACK_AGENT_GATEWAY_CONFIGURATION_ERROR =
  'Slack agent gateway is not configured.'
const SLACK_AGENT_GATEWAY_CONFIGURATION_INVALID_ERROR =
  'Slack agent gateway configuration is invalid.'
const SLACK_AGENT_GATEWAY_REQUEST_ERROR = 'Slack agent gateway request failed.'

function getSlackAgentGatewayHost(): string {
  return process.env.SLACK_AGENT_GATEWAY_HOST ?? ''
}

function getSlackAgentGatewayApiKey(): string {
  return process.env.SLACK_AGENT_GATEWAY_API_KEY ?? ''
}

function normalizeSlackAgentGatewayBaseUrl(host: string): string | undefined {
  const trimmedHost = host.trim()

  if (!trimmedHost) {
    return undefined
  }

  try {
    const url = new URL(trimmedHost)

    if (url.protocol !== 'http:' && url.protocol !== 'https:') {
      return undefined
    }

    return url.toString().replace(/\/$/, '')
  } catch {
    return undefined
  }
}

export function isSlackAgentGatewayConfigured(): boolean {
  return Boolean(
    normalizeSlackAgentGatewayBaseUrl(getSlackAgentGatewayHost()) &&
      getSlackAgentGatewayApiKey().trim()
  )
}

function baseUrl(): string {
  const host = getSlackAgentGatewayHost()
  const normalized = normalizeSlackAgentGatewayBaseUrl(host)

  if (normalized) {
    return normalized
  }

  if (!host.trim()) {
    throw new Error(SLACK_AGENT_GATEWAY_CONFIGURATION_ERROR)
  }

  throw new Error(SLACK_AGENT_GATEWAY_CONFIGURATION_INVALID_ERROR)
}

function parseJsonSlackAgentGatewayResponse<T>(
  text: string,
  schema: z.ZodType<T>
): T {
  if (!text.trim()) {
    return schema.parse(undefined)
  }

  return schema.parse(JSON.parse(text))
}

async function slackAgentGatewayFetch<T>(
  path: string,
  schema: z.ZodType<T>,
  init?: RequestInit
): Promise<T> {
  const apiKey = getSlackAgentGatewayApiKey()

  if (!apiKey) {
    throw new Error(SLACK_AGENT_GATEWAY_CONFIGURATION_ERROR)
  }

  const response = await fetch(`${baseUrl()}${path}`, {
    ...init,
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': apiKey,
      ...init?.headers,
    },
  })

  if (!response.ok) {
    throw new SlackAgentGatewayHttpError(
      response.status,
      SLACK_AGENT_GATEWAY_REQUEST_ERROR
    )
  }

  const text = await response.text()
  return parseJsonSlackAgentGatewayResponse(text, schema)
}

function wait(delayMs: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, delayMs)
  })
}

export async function getSlackGatewayBotStatus(
  agentName: string,
  options?: GetSlackGatewayBotStatusOptions
): Promise<SlackGatewayStatus> {
  const retryDelays = options?.notFoundRetryDelaysMs ?? []

  for (let attempt = 0; attempt <= retryDelays.length; attempt += 1) {
    try {
      const response =
        await slackAgentGatewayFetch<SlackGatewayBotStatusResponse>(
          `/api/v1/bot-father/status/${encodeURIComponent(agentName)}`,
          slackGatewayBotStatusResponseSchema
        )
      return sanitizeSlackGatewayStatus(response)
    } catch (error) {
      if (
        options?.treat404AsEmpty === false ||
        !isSlackAgentGatewayHttpError(error, 404)
      ) {
        throw error
      }

      const retryDelay = retryDelays[attempt]
      if (retryDelay === undefined) {
        return getEmptySlackGatewayStatus()
      }

      await wait(retryDelay)
    }
  }

  return getEmptySlackGatewayStatus()
}

export async function requestSlackGatewayBot(
  request: BotCreationRequest
): Promise<SlackGatewayBotCreationResponse> {
  return slackAgentGatewayFetch(
    '/api/v1/bot-father/request',
    slackGatewayBotCreationResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(botCreationRequestSchema.parse(request)),
    }
  )
}
