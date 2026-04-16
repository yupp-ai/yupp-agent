import type { SlackGatewayRequestStatus } from './types'

export type SlackInstallOwnershipState =
  | 'checking'
  | 'error'
  | 'owner'
  | 'read-only'
  | 'shared-global'

type SlackInstallEligibilityInput = {
  hasSlackBot: boolean
  isSlackGatewayEnabled: boolean
  ownershipState: SlackInstallOwnershipState
  requestStatus?: SlackGatewayRequestStatus
}

export function isRetryableSlackGatewayRequestStatus(
  status?: SlackGatewayRequestStatus
): boolean {
  return status === 'DENIED' || status === 'FAILED'
}

export function getSlackInstallEligibility(
  input: SlackInstallEligibilityInput
) {
  const isOwner = input.ownershipState === 'owner'
  const isCheckingOwnership = input.ownershipState === 'checking'
  const hasOwnershipError = input.ownershipState === 'error'
  const hasPendingRequest = Boolean(input.requestStatus)
  const hasRetryableRequest = isRetryableSlackGatewayRequestStatus(
    input.requestStatus
  )

  const isBlockedByGateway =
    !input.hasSlackBot &&
    !hasPendingRequest &&
    !input.isSlackGatewayEnabled &&
    !isOwner &&
    !hasOwnershipError &&
    !isCheckingOwnership
  const isWaitingForOwnership =
    !input.hasSlackBot &&
    !hasPendingRequest &&
    !input.isSlackGatewayEnabled &&
    isCheckingOwnership
  const isBlockedByOwnershipError =
    !input.hasSlackBot &&
    !hasPendingRequest &&
    !input.isSlackGatewayEnabled &&
    hasOwnershipError
  const isRetryBlockedByGateway =
    hasRetryableRequest &&
    !input.isSlackGatewayEnabled &&
    !isOwner &&
    !hasOwnershipError &&
    !isCheckingOwnership
  const isRetryWaitingForOwnership =
    hasRetryableRequest && !input.isSlackGatewayEnabled && isCheckingOwnership
  const isRetryBlockedByOwnershipError =
    hasRetryableRequest && !input.isSlackGatewayEnabled && hasOwnershipError
  const shouldRefreshOwnership =
    isWaitingForOwnership ||
    isBlockedByOwnershipError ||
    isRetryWaitingForOwnership ||
    isRetryBlockedByOwnershipError
  const canInstall =
    !input.hasSlackBot &&
    !hasPendingRequest &&
    !isBlockedByOwnershipError &&
    !isWaitingForOwnership &&
    !isBlockedByGateway
  const canRetryInstall =
    hasRetryableRequest &&
    !isRetryBlockedByGateway &&
    !isRetryWaitingForOwnership &&
    !isRetryBlockedByOwnershipError

  return {
    canInstall,
    canRetryInstall,
    hasOwnershipError,
    hasRetryableRequest,
    isBlockedByGateway,
    isBlockedByOwnershipError,
    isCheckingOwnership,
    isOwner,
    isRetryBlockedByGateway,
    isRetryBlockedByOwnershipError,
    isRetryWaitingForOwnership,
    isWaitingForOwnership,
    shouldRefreshOwnership,
  }
}
