import { z } from 'zod'

function toOptionalString(value: string | null | undefined) {
  return value?.trim() || undefined
}

const optionalHttpUrlSchema = z.preprocess(
  (value) => {
    if (value == null) {
      return undefined
    }

    return typeof value === 'string' ? toOptionalString(value) : value
  },
  z
    .string()
    .url()
    .refine((value) => {
      const url = new URL(value)
      return url.protocol === 'http:' || url.protocol === 'https:'
    }, 'Expected an http(s) URL')
    .optional()
)

function parseOptionalHttpUrl(value: string | null | undefined) {
  return optionalHttpUrlSchema.parse(value)
}

export const SLACK_GATEWAY_REQUEST_STATUS_VALUES = [
  'PENDING',
  'APPROVED',
  'AWAITING_INSTALLATION',
  'COMPLETED',
  'DENIED',
  'FAILED',
] as const

export const SLACK_GATEWAY_ACTIVE_REQUEST_STATUS_VALUES = [
  'PENDING',
  'APPROVED',
  'AWAITING_INSTALLATION',
] as const

export const SLACK_GATEWAY_RETRYABLE_REQUEST_STATUS_VALUES = [
  'DENIED',
  'FAILED',
] as const

export const slackGatewayRequestStatusSchema = z.enum(
  SLACK_GATEWAY_REQUEST_STATUS_VALUES
)

export const slackGatewaySanitizedPendingRequestSchema = z.object({
  requestId: z.string(),
  status: slackGatewayRequestStatusSchema,
  displayName: z.string(),
  oauthInstallUrl: optionalHttpUrlSchema,
  createdAt: z.string(),
  updatedAt: z.string(),
  error: z.string().optional(),
})

export const slackGatewaySanitizedStatusSchema = z.object({
  hasSlackBot: z.boolean(),
  slackAppId: z.string().optional(),
  slackName: z.string().optional(),
  pendingRequest: slackGatewaySanitizedPendingRequestSchema.optional(),
})

export type SlackGatewayRequestStatus = z.infer<
  typeof slackGatewayRequestStatusSchema
>
export type SlackGatewayPendingRequest = z.infer<
  typeof slackGatewaySanitizedPendingRequestSchema
>
export type SlackGatewayStatus = z.infer<
  typeof slackGatewaySanitizedStatusSchema
>

export const botCreationRequestSchema = z.object({
  agent_name: z.string().min(1),
  slack_name: z.string().min(1),
  display_name: z.string().min(1),
  requested_by: z.string().min(1),
  requested_by_email: z.string().email(),
  description: z.string().min(1).optional(),
})

export type BotCreationRequest = z.infer<typeof botCreationRequestSchema>

export const botCreationRecordSchema = z
  .object({
    request_id: z.string().min(1),
    status: slackGatewayRequestStatusSchema,
    display_name: z.string().nullish(),
    agent_name: z.string().nullish(),
    slack_name: z.string().nullish(),
    slack_app_id: z.string().nullish(),
    oauth_install_url: z.string().nullish(),
    created_at: z.string().min(1),
    updated_at: z.string().min(1),
    error: z.string().nullish(),
  })
  .passthrough()

export const slackGatewayStatusEnvelopeSchema = z
  .object({
    has_slack_bot: z.boolean().optional(),
    slack_app_id: z.string().nullish(),
    slack_name: z.string().nullish(),
    pending_request: botCreationRecordSchema.nullish(),
  })
  .refine(
    (value) =>
      value.has_slack_bot !== undefined ||
      Boolean(toOptionalString(value.slack_app_id)) ||
      Boolean(toOptionalString(value.slack_name)) ||
      value.pending_request != null,
    {
      message: 'Expected Slack gateway status fields',
    }
  )
  .passthrough()

export const slackGatewayBotStatusResponseSchema =
  slackGatewayStatusEnvelopeSchema

export const slackGatewayBotCreationResponseSchema = z
  .object({
    request_id: z.string().min(1),
    status: slackGatewayRequestStatusSchema,
    message: z.string().min(1),
  })
  .passthrough()

export type BotCreationRecord = z.infer<typeof botCreationRecordSchema>
export type SlackGatewayBotStatusResponse = z.infer<
  typeof slackGatewayBotStatusResponseSchema
>
export type SlackGatewayBotCreationResponse = z.infer<
  typeof slackGatewayBotCreationResponseSchema
>

export const slackGatewayPendingRequestSchema =
  slackGatewaySanitizedPendingRequestSchema
export const slackGatewayStatusSchema = slackGatewaySanitizedStatusSchema
const EMPTY_SLACK_GATEWAY_STATUS: SlackGatewayStatus = { hasSlackBot: false }

export function getEmptySlackGatewayStatus(): SlackGatewayStatus {
  return { ...EMPTY_SLACK_GATEWAY_STATUS }
}

export function createPendingSlackGatewayStatus(input: {
  agentName: string
  displayName?: string | null
  createdAt?: string
  updatedAt?: string
  oauthInstallUrl?: string | null
  requestId?: string
  status?: SlackGatewayRequestStatus
}): SlackGatewayStatus {
  const createdAt = input.createdAt ?? new Date().toISOString()
  const updatedAt = input.updatedAt ?? createdAt
  const oauthInstallUrl = parseOptionalHttpUrl(input.oauthInstallUrl)

  return slackGatewaySanitizedStatusSchema.parse({
    hasSlackBot: false,
    pendingRequest: {
      requestId: input.requestId ?? `pending-sync:${input.agentName}`,
      status: input.status ?? 'PENDING',
      displayName: toOptionalString(input.displayName) ?? input.agentName,
      createdAt,
      updatedAt,
      ...(oauthInstallUrl ? { oauthInstallUrl } : {}),
    },
  })
}

export function isSlackGatewayActiveRequestStatus(
  status: SlackGatewayRequestStatus
): boolean {
  return (
    SLACK_GATEWAY_ACTIVE_REQUEST_STATUS_VALUES as readonly string[]
  ).includes(status)
}

export function isSlackGatewayRetryableRequestStatus(
  status: SlackGatewayRequestStatus
): boolean {
  return (
    SLACK_GATEWAY_RETRYABLE_REQUEST_STATUS_VALUES as readonly string[]
  ).includes(status)
}

export function hasActiveSlackGatewayRequest(
  status: SlackGatewayStatus
): boolean {
  return status.pendingRequest
    ? isSlackGatewayActiveRequestStatus(status.pendingRequest.status)
    : false
}

export function hasRetryableSlackGatewayRequest(
  status: SlackGatewayStatus
): boolean {
  return status.pendingRequest
    ? isSlackGatewayRetryableRequestStatus(status.pendingRequest.status)
    : false
}

function normalizeCompletedSlackGatewayStatus(
  status: SlackGatewayStatus
): SlackGatewayStatus {
  if (status.hasSlackBot || status.pendingRequest?.status !== 'COMPLETED') {
    return status
  }

  return slackGatewaySanitizedStatusSchema.parse({
    ...status,
    hasSlackBot: true,
  })
}

export function normalizeSlackGatewayStatus(
  parsed: SlackGatewayBotStatusResponse
): SlackGatewayStatus {
  const pendingRequestRecord: BotCreationRecord | undefined =
    parsed.pending_request ?? undefined

  const topLevelSlackAppId = toOptionalString(parsed.slack_app_id)
  const topLevelSlackName = toOptionalString(parsed.slack_name)
  const pendingSlackAppId = toOptionalString(pendingRequestRecord?.slack_app_id)
  const pendingSlackName = toOptionalString(pendingRequestRecord?.slack_name)
  const pendingRequestStatus = pendingRequestRecord?.status
  const hasPendingRequest = pendingRequestStatus !== undefined
  const hasSlackBot =
    pendingRequestStatus === 'COMPLETED' ||
    Boolean(parsed.has_slack_bot) ||
    (!hasPendingRequest &&
      Boolean(
        topLevelSlackAppId ||
          pendingSlackAppId ||
          topLevelSlackName ||
          pendingSlackName
      ))

  const slackAppId = hasSlackBot
    ? (topLevelSlackAppId ?? pendingSlackAppId)
    : undefined
  const slackName = hasSlackBot
    ? (topLevelSlackName ?? pendingSlackName)
    : undefined

  const oauthInstallUrl = parseOptionalHttpUrl(
    pendingRequestRecord?.oauth_install_url
  )
  const pendingRequest = pendingRequestRecord
    ? slackGatewaySanitizedPendingRequestSchema.parse({
        requestId: pendingRequestRecord.request_id,
        status: pendingRequestRecord.status,
        displayName:
          toOptionalString(pendingRequestRecord.display_name) ??
          pendingSlackName ??
          toOptionalString(pendingRequestRecord.agent_name) ??
          'Slack bot',
        createdAt: pendingRequestRecord.created_at,
        updatedAt: pendingRequestRecord.updated_at,
        ...(oauthInstallUrl ? { oauthInstallUrl } : {}),
        ...(toOptionalString(pendingRequestRecord.error)
          ? { error: toOptionalString(pendingRequestRecord.error) }
          : {}),
      })
    : undefined

  return normalizeCompletedSlackGatewayStatus(
    slackGatewaySanitizedStatusSchema.parse({
      hasSlackBot,
      ...(slackAppId ? { slackAppId } : {}),
      ...(slackName ? { slackName } : {}),
      ...(pendingRequest ? { pendingRequest } : {}),
    })
  )
}

export function sanitizeSlackGatewayStatus(input: unknown): SlackGatewayStatus {
  const sanitized = tryGetSlackGatewayStatus(input)
  if (sanitized) {
    return sanitized
  }

  return normalizeSlackGatewayStatus(
    slackGatewayBotStatusResponseSchema.parse(input)
  )
}

export function tryGetSlackGatewayStatus(
  input: unknown
): SlackGatewayStatus | undefined {
  const sanitized = slackGatewaySanitizedStatusSchema.safeParse(input)
  if (sanitized.success) {
    return normalizeCompletedSlackGatewayStatus(sanitized.data)
  }

  const parsed = slackGatewayStatusEnvelopeSchema.safeParse(input)
  if (parsed.success) {
    return normalizeSlackGatewayStatus(parsed.data)
  }

  return undefined
}
