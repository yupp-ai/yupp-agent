import * as bunTest from 'bun:test'

const { describe, expect, it } = bunTest

;(
  bunTest as unknown as {
    mock: { module(path: string, factory: () => unknown): void }
  }
).mock.module('server-only', () => ({}))

const originalFetch = globalThis.fetch
const originalHost = process.env.SLACK_AGENT_GATEWAY_HOST
const originalApiKey = process.env.SLACK_AGENT_GATEWAY_API_KEY

function restoreEnvironment() {
  globalThis.fetch = originalFetch

  if (originalHost === undefined) {
    delete process.env.SLACK_AGENT_GATEWAY_HOST
  } else {
    process.env.SLACK_AGENT_GATEWAY_HOST = originalHost
  }

  if (originalApiKey === undefined) {
    delete process.env.SLACK_AGENT_GATEWAY_API_KEY
  } else {
    process.env.SLACK_AGENT_GATEWAY_API_KEY = originalApiKey
  }
}

describe('slack agent gateway server client', () => {
  it('treats hosts without an http scheme as unavailable', async () => {
    process.env.SLACK_AGENT_GATEWAY_HOST = 'slack-gateway.internal'
    process.env.SLACK_AGENT_GATEWAY_API_KEY = 'test-api-key'

    const { isSlackAgentGatewayConfigured, requestSlackGatewayBot } =
      await import('@/lib/slack-agent-gateway/server/client')

    try {
      expect(isSlackAgentGatewayConfigured()).toBe(false)

      await requestSlackGatewayBot({
        agent_name: 'ops-bot',
        slack_name: 'ops-bot',
        display_name: 'Ops Bot',
        requested_by: 'war-room',
        requested_by_email: 'ops@example.com',
      })
      throw new Error(
        'Expected requestSlackGatewayBot to reject an invalid gateway host'
      )
    } catch (error) {
      expect(error).toMatchObject({
        message: 'Slack agent gateway configuration is invalid.',
      })
    } finally {
      restoreEnvironment()
    }
  })

  it('retries 404 responses before returning a synchronized pending status', async () => {
    process.env.SLACK_AGENT_GATEWAY_HOST = 'http://slack-gateway.test'
    process.env.SLACK_AGENT_GATEWAY_API_KEY = 'test-api-key'
    let requestCount = 0

    globalThis.fetch = (async () => {
      requestCount += 1

      if (requestCount < 3) {
        return new Response(JSON.stringify({ detail: 'Not found' }), {
          status: 404,
          headers: {
            'Content-Type': 'application/json',
          },
        })
      }

      return new Response(
        JSON.stringify({
          has_slack_bot: false,
          pending_request: {
            request_id: 'req-123',
            status: 'PENDING',
            display_name: 'Race Agent',
            slack_name: 'race-agent',
            created_at: '2026-03-19T10:00:00Z',
            updated_at: '2026-03-19T10:00:05Z',
            error: null,
          },
        }),
        {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
          },
        }
      )
    }) as typeof fetch

    const { getSlackGatewayBotStatus } = await import(
      '@/lib/slack-agent-gateway/server/client'
    )

    try {
      const status = await getSlackGatewayBotStatus('race-agent', {
        notFoundRetryDelaysMs: [0, 0],
      })

      expect(status).toEqual({
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-123',
          status: 'PENDING',
          displayName: 'Race Agent',
          createdAt: '2026-03-19T10:00:00Z',
          updatedAt: '2026-03-19T10:00:05Z',
        },
      })
      expect(requestCount).toBe(3)
    } finally {
      restoreEnvironment()
    }
  })

  it('throws the 404 when callers disable the empty-status fallback', async () => {
    process.env.SLACK_AGENT_GATEWAY_HOST = 'http://slack-gateway.test'
    process.env.SLACK_AGENT_GATEWAY_API_KEY = 'test-api-key'
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ detail: 'Not found' }), {
        status: 404,
        headers: {
          'Content-Type': 'application/json',
        },
      })) as typeof fetch

    const { getSlackGatewayBotStatus, isSlackAgentGatewayHttpError } =
      await import('@/lib/slack-agent-gateway/server/client')

    try {
      await getSlackGatewayBotStatus('missing-agent', {
        treat404AsEmpty: false,
      })
      throw new Error('Expected getSlackGatewayBotStatus to throw on 404')
    } catch (error) {
      expect(isSlackAgentGatewayHttpError(error, 404)).toBe(true)
    } finally {
      restoreEnvironment()
    }
  })

  it('parses bot creation responses as JSON', async () => {
    process.env.SLACK_AGENT_GATEWAY_HOST = 'http://slack-gateway.test'
    process.env.SLACK_AGENT_GATEWAY_API_KEY = 'test-api-key'
    let capturedUrl: string | undefined
    let capturedInit: RequestInit | undefined

    globalThis.fetch = (async (input, init) => {
      capturedUrl = String(input)
      capturedInit = init

      return new Response(
        JSON.stringify({
          request_id: 'req-123',
          status: 'PENDING',
          message: 'queued',
        }),
        {
          status: 202,
          headers: {
            'Content-Type': 'application/json',
          },
        }
      )
    }) as typeof fetch

    const { requestSlackGatewayBot } = await import(
      '@/lib/slack-agent-gateway/server/client'
    )

    try {
      const response = await requestSlackGatewayBot({
        agent_name: 'ops-bot',
        slack_name: 'ops-bot',
        display_name: 'Ops Bot',
        requested_by: 'war-room',
        requested_by_email: 'ops@example.com',
      })

      expect(response).toEqual({
        request_id: 'req-123',
        status: 'PENDING',
        message: 'queued',
      })
      expect(capturedUrl).toBe(
        'http://slack-gateway.test/api/v1/bot-father/request'
      )
      expect(capturedInit?.method).toBe('POST')
    } finally {
      restoreEnvironment()
    }
  })

  it('rejects non-JSON success bodies when requesting bot creation', async () => {
    process.env.SLACK_AGENT_GATEWAY_HOST = 'http://slack-gateway.test'
    process.env.SLACK_AGENT_GATEWAY_API_KEY = 'test-api-key'
    globalThis.fetch = (async () =>
      new Response('queued', {
        status: 202,
        headers: {
          'Content-Type': 'text/plain',
        },
      })) as typeof fetch

    const { requestSlackGatewayBot } = await import(
      '@/lib/slack-agent-gateway/server/client'
    )

    try {
      await requestSlackGatewayBot({
        agent_name: 'ops-bot',
        slack_name: 'ops-bot',
        display_name: 'Ops Bot',
        requested_by: 'war-room',
        requested_by_email: 'ops@example.com',
      })
      throw new Error(
        'Expected requestSlackGatewayBot to reject a non-JSON 2xx response'
      )
    } catch (error) {
      expect(error instanceof SyntaxError).toBe(true)
    } finally {
      restoreEnvironment()
    }
  })

  it('keeps strict JSON parsing for status reads', async () => {
    process.env.SLACK_AGENT_GATEWAY_HOST = 'http://slack-gateway.test'
    process.env.SLACK_AGENT_GATEWAY_API_KEY = 'test-api-key'
    globalThis.fetch = (async () =>
      new Response('queued', {
        status: 200,
        headers: {
          'Content-Type': 'text/plain',
        },
      })) as typeof fetch

    const { getSlackGatewayBotStatus } = await import(
      '@/lib/slack-agent-gateway/server/client'
    )

    try {
      await getSlackGatewayBotStatus('ops-bot', {
        treat404AsEmpty: false,
      })
      throw new Error(
        'Expected getSlackGatewayBotStatus to reject a non-JSON 2xx response'
      )
    } catch (error) {
      expect(error instanceof SyntaxError).toBe(true)
    } finally {
      restoreEnvironment()
    }
  })

  it('does not expose upstream response details in error messages', async () => {
    process.env.SLACK_AGENT_GATEWAY_HOST = 'http://slack-gateway.test'
    process.env.SLACK_AGENT_GATEWAY_API_KEY = 'test-api-key'
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ detail: 'secret upstream detail' }), {
        status: 500,
        headers: {
          'Content-Type': 'application/json',
        },
      })) as typeof fetch

    const { getSlackGatewayBotStatus, isSlackAgentGatewayHttpError } =
      await import('@/lib/slack-agent-gateway/server/client')

    try {
      await getSlackGatewayBotStatus('ops-bot', {
        treat404AsEmpty: false,
      })
      throw new Error('Expected getSlackGatewayBotStatus to throw on 500')
    } catch (error) {
      expect(isSlackAgentGatewayHttpError(error, 500)).toBe(true)
      expect(error).toMatchObject({
        message: 'Slack agent gateway request failed.',
      })
    } finally {
      restoreEnvironment()
    }
  })
})
