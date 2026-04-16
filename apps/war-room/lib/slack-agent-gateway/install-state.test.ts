import * as bunTest from 'bun:test'
import { getSlackInstallEligibility } from './install-state'

const { describe, expect, it } = bunTest

describe('slack gateway install state', () => {
  it('blocks retry when a read-only operator cannot re-enable the gateway', () => {
    expect(
      getSlackInstallEligibility({
        hasSlackBot: false,
        isSlackGatewayEnabled: false,
        ownershipState: 'read-only',
        requestStatus: 'FAILED',
      })
    ).toMatchObject({
      canRetryInstall: false,
      isRetryBlockedByGateway: true,
    })
  })

  it('allows retry when the owner can re-enable the gateway', () => {
    expect(
      getSlackInstallEligibility({
        hasSlackBot: false,
        isSlackGatewayEnabled: false,
        ownershipState: 'owner',
        requestStatus: 'DENIED',
      })
    ).toMatchObject({
      canRetryInstall: true,
      isRetryBlockedByGateway: false,
    })
  })

  it('refreshes ownership while an initial install is waiting on ownership', () => {
    expect(
      getSlackInstallEligibility({
        hasSlackBot: false,
        isSlackGatewayEnabled: false,
        ownershipState: 'checking',
      })
    ).toMatchObject({
      isWaitingForOwnership: true,
      shouldRefreshOwnership: true,
    })
  })

  it('refreshes ownership for retry states blocked on ownership lookups', () => {
    expect(
      getSlackInstallEligibility({
        hasSlackBot: false,
        isSlackGatewayEnabled: false,
        ownershipState: 'error',
        requestStatus: 'FAILED',
      })
    ).toMatchObject({
      isRetryBlockedByOwnershipError: true,
      shouldRefreshOwnership: true,
    })
  })

  it('blocks initial install for shared agents until slack is enabled elsewhere', () => {
    expect(
      getSlackInstallEligibility({
        hasSlackBot: false,
        isSlackGatewayEnabled: false,
        ownershipState: 'shared-global',
      })
    ).toMatchObject({
      canInstall: false,
      isBlockedByGateway: true,
    })
  })
})
