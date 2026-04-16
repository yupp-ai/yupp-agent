import { describe, expect, it } from 'bun:test'
import {
  buildSubmittedSlackGatewayStatus,
  resolvePostSubmitSlackGatewayStatus,
  resolveVisibleSlackGatewayStatus,
  submitSlackGatewayRequest,
} from './post-submit'
import { getEmptySlackGatewayStatus } from './types'

describe('slack gateway post-submit status', () => {
  it('uses the creation response to seed the optimistic pending status', () => {
    const result = buildSubmittedSlackGatewayStatus({
      agentName: 'ops-bot',
      displayName: 'Ops Bot',
      requestedAt: '2026-03-19T10:00:00Z',
      requestResponse: {
        request_id: 'req-123',
        status: 'PENDING',
        message: 'queued',
      },
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-123',
        status: 'PENDING',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:00Z',
      },
    })
  })

  it('preserves the submitted request when the follow-up status is empty', () => {
    const fallbackStatus = buildSubmittedSlackGatewayStatus({
      agentName: 'ops-bot',
      displayName: 'Ops Bot',
      requestedAt: '2026-03-19T10:00:00Z',
      requestResponse: {
        request_id: 'req-123',
        status: 'PENDING',
        message: 'queued',
      },
    })

    const result = resolvePostSubmitSlackGatewayStatus({
      fallbackStatus,
      polledStatus: getEmptySlackGatewayStatus(),
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-123',
        status: 'PENDING',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:00Z',
      },
    })
  })

  it('uses the polled status once the gateway returns request state', () => {
    const fallbackStatus = buildSubmittedSlackGatewayStatus({
      agentName: 'ops-bot',
      displayName: 'Ops Bot',
      requestedAt: '2026-03-19T10:00:00Z',
    })

    const result = resolvePostSubmitSlackGatewayStatus({
      fallbackStatus,
      polledStatus: {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-456',
          status: 'AWAITING_INSTALLATION',
          displayName: 'Ops Bot',
          oauthInstallUrl: 'https://slack.com/oauth/install',
          createdAt: '2026-03-19T10:01:00Z',
          updatedAt: '2026-03-19T10:02:00Z',
        },
      },
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-456',
        status: 'AWAITING_INSTALLATION',
        displayName: 'Ops Bot',
        oauthInstallUrl: 'https://slack.com/oauth/install',
        createdAt: '2026-03-19T10:01:00Z',
        updatedAt: '2026-03-19T10:02:00Z',
      },
    })
  })

  it('keeps showing the last known pending state while follow-up polls are empty', () => {
    const result = resolveVisibleSlackGatewayStatus({
      currentStatus: getEmptySlackGatewayStatus(),
      lastKnownStatus: {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-456',
          status: 'AWAITING_INSTALLATION',
          displayName: 'Ops Bot',
          oauthInstallUrl: 'https://slack.com/oauth/install',
          createdAt: '2026-03-19T10:01:00Z',
          updatedAt: '2026-03-19T10:02:00Z',
        },
      },
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-456',
        status: 'AWAITING_INSTALLATION',
        displayName: 'Ops Bot',
        oauthInstallUrl: 'https://slack.com/oauth/install',
        createdAt: '2026-03-19T10:01:00Z',
        updatedAt: '2026-03-19T10:02:00Z',
      },
    })
  })

  it('preserves the last known install URL while awaiting installation', () => {
    const result = resolveVisibleSlackGatewayStatus({
      currentStatus: {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-456',
          status: 'AWAITING_INSTALLATION',
          displayName: 'Ops Bot',
          createdAt: '2026-03-19T10:01:00Z',
          updatedAt: '2026-03-19T10:03:00Z',
        },
      },
      lastKnownStatus: {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-456',
          status: 'AWAITING_INSTALLATION',
          displayName: 'Ops Bot',
          oauthInstallUrl: 'https://slack.com/oauth/install',
          createdAt: '2026-03-19T10:01:00Z',
          updatedAt: '2026-03-19T10:02:00Z',
        },
      },
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-456',
        status: 'AWAITING_INSTALLATION',
        displayName: 'Ops Bot',
        oauthInstallUrl: 'https://slack.com/oauth/install',
        createdAt: '2026-03-19T10:01:00Z',
        updatedAt: '2026-03-19T10:03:00Z',
      },
    })
  })

  it('treats duplicate submit conflicts as an existing pending request', async () => {
    let capturedFallbackStatus: unknown

    const result = await submitSlackGatewayRequest({
      agentName: 'ops-bot',
      displayName: 'Ops Bot',
      requestedAt: '2026-03-19T10:00:00Z',
      submitRequest: async () => {
        throw {
          status: 409,
          message: 'Slack agent gateway 409: Request already exists',
        }
      },
      getPostSubmitStatus: async (fallbackStatus) => {
        capturedFallbackStatus = fallbackStatus

        return {
          hasSlackBot: false,
          pendingRequest: {
            requestId: 'req-789',
            status: 'PENDING',
            displayName: 'Ops Bot',
            createdAt: '2026-03-19T10:00:00Z',
            updatedAt: '2026-03-19T10:00:05Z',
          },
        }
      },
      isDuplicateRequestError: (error) =>
        typeof error === 'object' &&
        error !== null &&
        'status' in error &&
        error.status === 409,
    })

    expect(capturedFallbackStatus).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'pending-sync:ops-bot',
        status: 'PENDING',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:00Z',
      },
    })
    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-789',
        status: 'PENDING',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:05Z',
      },
    })
  })
})
