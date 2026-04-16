import { describe, expect, it } from 'bun:test'
import {
  createPendingSlackGatewayStatus,
  getEmptySlackGatewayStatus,
  hasActiveSlackGatewayRequest,
  hasRetryableSlackGatewayRequest,
  normalizeSlackGatewayStatus,
  sanitizeSlackGatewayStatus,
  tryGetSlackGatewayStatus,
} from './types'

describe('slack agent gateway types', () => {
  it('normalizes an envelope response into the sanitized status shape', () => {
    const result = normalizeSlackGatewayStatus({
      has_slack_bot: false,
      pending_request: {
        request_id: 'req-123',
        status: 'AWAITING_INSTALLATION',
        display_name: 'Deploy Bot',
        slack_name: 'deploy-bot',
        oauth_install_url: 'https://slack.com/oauth/install',
        created_at: '2026-03-18T12:00:00Z',
        updated_at: '2026-03-18T12:05:00Z',
        error: null,
      },
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-123',
        status: 'AWAITING_INSTALLATION',
        displayName: 'Deploy Bot',
        oauthInstallUrl: 'https://slack.com/oauth/install',
        createdAt: '2026-03-18T12:00:00Z',
        updatedAt: '2026-03-18T12:05:00Z',
      },
    })
  })

  it('keeps awaiting-installation requests pending even when the app id exists', () => {
    const result = normalizeSlackGatewayStatus({
      has_slack_bot: false,
      pending_request: {
        request_id: 'req-456a',
        status: 'AWAITING_INSTALLATION',
        display_name: 'Deploy Bot',
        agent_name: 'deploy-bot',
        slack_name: 'deploy-bot',
        slack_app_id: 'A12345678',
        oauth_install_url: 'https://slack.com/oauth/install',
        created_at: '2026-03-18T12:00:00Z',
        updated_at: '2026-03-18T12:05:00Z',
        error: null,
      },
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-456a',
        status: 'AWAITING_INSTALLATION',
        displayName: 'Deploy Bot',
        oauthInstallUrl: 'https://slack.com/oauth/install',
        createdAt: '2026-03-18T12:00:00Z',
        updatedAt: '2026-03-18T12:05:00Z',
      },
    })
  })

  it('treats completed requests as live even before the gateway sync finishes', () => {
    expect(
      normalizeSlackGatewayStatus({
        has_slack_bot: false,
        pending_request: {
          request_id: 'req-457',
          status: 'COMPLETED',
          display_name: 'Deploy Bot',
          slack_name: 'deploy-bot',
          created_at: '2026-03-18T12:00:00Z',
          updated_at: '2026-03-18T12:05:00Z',
          error: null,
        },
      })
    ).toEqual({
      hasSlackBot: true,
      slackName: 'deploy-bot',
      pendingRequest: {
        requestId: 'req-457',
        status: 'COMPLETED',
        displayName: 'Deploy Bot',
        createdAt: '2026-03-18T12:00:00Z',
        updatedAt: '2026-03-18T12:05:00Z',
      },
    })
  })

  it('returns an empty status and classifies active and retryable states', () => {
    expect(getEmptySlackGatewayStatus()).toEqual({ hasSlackBot: false })

    expect(
      hasActiveSlackGatewayRequest({
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-789',
          status: 'PENDING',
          displayName: 'Ops Bot',
          createdAt: '2026-03-18T12:00:00Z',
          updatedAt: '2026-03-18T12:05:00Z',
        },
      })
    ).toBe(true)

    expect(
      hasRetryableSlackGatewayRequest({
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-790',
          status: 'FAILED',
          displayName: 'Ops Bot',
          createdAt: '2026-03-18T12:00:00Z',
          updatedAt: '2026-03-18T12:05:00Z',
        },
      })
    ).toBe(true)
  })

  it('creates a sanitized optimistic pending status', () => {
    expect(
      createPendingSlackGatewayStatus({
        agentName: 'ops-bot',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
      })
    ).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'pending-sync:ops-bot',
        status: 'PENDING',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:00Z',
      },
    })
  })

  it('fails fast when the backend returns an invalid install url', () => {
    expect(() =>
      sanitizeSlackGatewayStatus({
        has_slack_bot: false,
        pending_request: {
          request_id: 'req-791',
          status: 'AWAITING_INSTALLATION',
          display_name: 'Ops Bot',
          agent_name: 'ops-bot',
          slack_name: 'ops-bot',
          oauth_install_url: 'javascript:alert(1)',
          created_at: '2026-03-19T10:00:00Z',
          updated_at: '2026-03-19T10:05:00Z',
        },
      })
    ).toThrow()
  })

  it('treats null install urls from the backend as absent', () => {
    expect(
      sanitizeSlackGatewayStatus({
        has_slack_bot: false,
        pending_request: {
          request_id: 'req-791a',
          status: 'AWAITING_INSTALLATION',
          display_name: 'Ops Bot',
          agent_name: 'ops-bot',
          slack_name: 'ops-bot',
          oauth_install_url: null,
          created_at: '2026-03-19T10:00:00Z',
          updated_at: '2026-03-19T10:05:00Z',
        },
      })
    ).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-791a',
        status: 'AWAITING_INSTALLATION',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:05:00Z',
      },
    })
  })

  it('upgrades completed sanitized input into a live bot state', () => {
    expect(
      tryGetSlackGatewayStatus({
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-792',
          status: 'COMPLETED',
          displayName: 'Ops Bot',
          createdAt: '2026-03-19T10:00:00Z',
          updatedAt: '2026-03-19T10:05:00Z',
        },
      })
    ).toEqual({
      hasSlackBot: true,
      pendingRequest: {
        requestId: 'req-792',
        status: 'COMPLETED',
        displayName: 'Ops Bot',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:05:00Z',
      },
    })
  })

  it('returns undefined for unrelated input', () => {
    expect(tryGetSlackGatewayStatus({ ok: true })).toBe(undefined)
  })
})
