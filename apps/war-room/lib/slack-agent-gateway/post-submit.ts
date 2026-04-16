import {
  createPendingSlackGatewayStatus,
  getEmptySlackGatewayStatus,
  type SlackGatewayBotCreationResponse,
  type SlackGatewayStatus,
} from './types'

function hasSlackGatewayState(status: SlackGatewayStatus): boolean {
  return status.hasSlackBot || Boolean(status.pendingRequest)
}

function mergeVisibleSlackGatewayStatus(input: {
  currentStatus: SlackGatewayStatus
  lastKnownStatus: SlackGatewayStatus
}): SlackGatewayStatus {
  const currentPendingRequest = input.currentStatus.pendingRequest
  const lastKnownPendingRequest = input.lastKnownStatus.pendingRequest

  if (
    !currentPendingRequest ||
    !lastKnownPendingRequest ||
    currentPendingRequest.requestId !== lastKnownPendingRequest.requestId ||
    currentPendingRequest.status !== 'AWAITING_INSTALLATION' ||
    lastKnownPendingRequest.status !== 'AWAITING_INSTALLATION' ||
    currentPendingRequest.oauthInstallUrl ||
    !lastKnownPendingRequest.oauthInstallUrl
  ) {
    return input.currentStatus
  }

  return {
    ...input.currentStatus,
    pendingRequest: {
      ...currentPendingRequest,
      oauthInstallUrl: lastKnownPendingRequest.oauthInstallUrl,
    },
  }
}

export function buildSubmittedSlackGatewayStatus(input: {
  agentName: string
  displayName: string
  requestedAt: string
  requestResponse?: SlackGatewayBotCreationResponse
}): SlackGatewayStatus {
  return createPendingSlackGatewayStatus({
    agentName: input.agentName,
    displayName: input.displayName,
    createdAt: input.requestedAt,
    updatedAt: input.requestedAt,
    requestId: input.requestResponse?.request_id,
    status: input.requestResponse?.status,
  })
}

export function resolvePostSubmitSlackGatewayStatus(input: {
  fallbackStatus: SlackGatewayStatus
  polledStatus: SlackGatewayStatus
}): SlackGatewayStatus {
  const currentStatus = hasSlackGatewayState(input.polledStatus)
    ? input.polledStatus
    : input.fallbackStatus

  return mergeVisibleSlackGatewayStatus({
    currentStatus,
    lastKnownStatus: input.fallbackStatus,
  })
}

export function resolveVisibleSlackGatewayStatus(input: {
  currentStatus?: SlackGatewayStatus
  lastKnownStatus?: SlackGatewayStatus | null
}): SlackGatewayStatus | undefined {
  if (!input.lastKnownStatus) {
    return input.currentStatus
  }

  return resolvePostSubmitSlackGatewayStatus({
    fallbackStatus: input.lastKnownStatus,
    polledStatus: input.currentStatus ?? getEmptySlackGatewayStatus(),
  })
}

export async function submitSlackGatewayRequest(input: {
  agentName: string
  displayName: string
  requestedAt: string
  submitRequest: () => Promise<SlackGatewayBotCreationResponse>
  getPostSubmitStatus: (
    fallbackStatus: SlackGatewayStatus
  ) => Promise<SlackGatewayStatus>
  isDuplicateRequestError: (error: unknown) => boolean
}): Promise<SlackGatewayStatus> {
  let requestResponse: SlackGatewayBotCreationResponse | undefined

  try {
    requestResponse = await input.submitRequest()
  } catch (error) {
    if (!input.isDuplicateRequestError(error)) {
      throw error
    }
  }

  // TODO: Once yupp-mind's canonical pending_request response is released, use
  // that record here for both success and duplicate-submit responses instead of
  // synthesizing an optimistic pending state.
  // The currently deployed gateway does not return that canonical record yet,
  // so we fall back to an optimistic pending state until the follow-up status
  // poll replaces it with the real backend state.
  const fallbackStatus = buildSubmittedSlackGatewayStatus({
    agentName: input.agentName,
    displayName: input.displayName,
    requestedAt: input.requestedAt,
    requestResponse,
  })

  return input.getPostSubmitStatus(fallbackStatus)
}
