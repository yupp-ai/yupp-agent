import * as bunTest from 'bun:test'
import type * as ahsClient from '@/lib/ahs/server/client'
import type * as slackGatewayClient from '@/lib/slack-agent-gateway/server/client'

const { describe, expect, it } = bunTest

;(
  bunTest as unknown as {
    mock: { module(path: string, factory: () => unknown): void }
  }
).mock.module('server-only', () => ({}))

;(
  bunTest as unknown as {
    mock: { module(path: string, factory: () => unknown): void }
  }
).mock.module('next/headers', () => ({
  headers: async () => new Headers(),
}))

;(
  bunTest as unknown as {
    mock: { module(path: string, factory: () => unknown): void }
  }
).mock.module('@/lib/auth/get-session', () => ({
  getInternalSession: async () => ({
    status: 'authenticated' as const,
    user: {
      id: 'operator-1',
      email: 'operator@yupp.ai',
    },
  }),
}))

let getSlackGatewayBotStatusMock: typeof slackGatewayClient.getSlackGatewayBotStatus
let isAhsHttpErrorMock: typeof ahsClient.isAhsHttpError
let isSlackAgentGatewayConfiguredMock: () => boolean
let requestSlackGatewayBotMock: typeof slackGatewayClient.requestSlackGatewayBot

class MockAhsHttpError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'AhsHttpError'
    this.status = status
  }
}

class MockSlackAgentGatewayHttpError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'SlackAgentGatewayHttpError'
    this.status = status
  }
}

let getAgentMock: typeof ahsClient.getAgent
let listAgentsMock: typeof ahsClient.listAgents
let editAgentMock: typeof ahsClient.editAgent

const { createSlackGatewayActions } = await import('./slack-gateway-actions')

const slackGatewayActions = createSlackGatewayActions({
  getAgent: (agentName) => getAgentMock(agentName),
  listAgents: (input) => listAgentsMock(input),
  editAgent: (input) => editAgentMock(input),
  getSlackGatewayBotStatus: (agentName, options) =>
    getSlackGatewayBotStatusMock(agentName, options),
  isSlackAgentGatewayConfigured: () => isSlackAgentGatewayConfiguredMock(),
  requestSlackGatewayBot: (request) => requestSlackGatewayBotMock(request),
  isAhsHttpError: (error, status) => isAhsHttpErrorMock(error, status),
  isSlackAgentGatewayHttpError: ((error, status) =>
    error instanceof MockSlackAgentGatewayHttpError &&
    (status === undefined ||
      error.status ===
        status)) as typeof slackGatewayClient.isSlackAgentGatewayHttpError,
})

function createCaller() {
  return slackGatewayActions.createCaller({
    session: {
      status: 'authenticated',
      user: {
        id: 'operator-1',
        email: 'operator@yupp.ai',
      },
    },
    ip: '127.0.0.1',
    userAgent: 'bun-test',
    hostname: 'localhost',
  } as never)
}

function createAgentDetail(overrides?: {
  allowedGateways?: string[]
  creatorUserId?: string | null
  description?: string
  displayName?: string
  name?: string
}) {
  return {
    agent: {
      name: overrides?.name ?? 'race-agent',
      display_name: overrides?.displayName ?? 'Race Agent',
      description: overrides?.description ?? 'Test agent',
      executor_type: 'codex',
      executor_model: null,
      llm_model: null,
      tool_permissions: {},
      allowed_subagents: [],
      default_repo: 'yupp-agent',
      max_turns: 5,
      max_budget_usd: 5,
      timeout_s: 60,
      sandbox_enabled: true,
      allowed_gateways: overrides?.allowedGateways ?? ['slack'],
      creator_user_id:
        overrides?.creatorUserId === undefined
          ? 'operator-1'
          : overrides.creatorUserId,
    },
    system_prompts: null,
  }
}

describe('slack gateway actions', () => {
  it('uses the operator email as requested_by_email while keeping requested_by fixed', async () => {
    const statusCalls: Array<{
      agentName: string
      options?: { notFoundRetryDelaysMs?: readonly number[] }
    }> = []
    let capturedRequest:
      | Parameters<typeof slackGatewayClient.requestSlackGatewayBot>[0]
      | undefined

    getSlackGatewayBotStatusMock = async (agentName, options) => {
      statusCalls.push({ agentName, options })

      if (statusCalls.length === 1) {
        return { hasSlackBot: false }
      }

      return {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-456',
          status: 'PENDING',
          displayName: 'Race Agent',
          createdAt: '2026-03-19T10:00:00Z',
          updatedAt: '2026-03-19T10:00:05Z',
        },
      }
    }
    requestSlackGatewayBotMock = async (request) => {
      capturedRequest = request
      return {
        request_id: 'req-created',
        status: 'PENDING',
        message: 'queued',
      }
    }
    getAgentMock = async () => createAgentDetail()
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called when Slack is enabled')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called when Slack is enabled')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    const result = await createCaller().installAgentBot({
      agentName: 'race-agent',
    })

    expect(capturedRequest).toEqual({
      agent_name: 'race-agent',
      slack_name: 'race-agent',
      display_name: 'Race Agent',
      requested_by: 'war-room',
      requested_by_email: 'operator@yupp.ai',
      description: 'Test agent',
    })
    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-456',
        status: 'PENDING',
        displayName: 'Race Agent',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:05Z',
      },
    })
    expect(statusCalls).toEqual([
      {
        agentName: 'race-agent',
        options: undefined,
      },
      {
        agentName: 'race-agent',
        options: {
          notFoundRetryDelaysMs: [250, 500, 1_000, 1_500],
        },
      },
    ])
  })

  it('skips list ownership fallback when the creator already matches the operator', async () => {
    let listAgentsCalls = 0
    let editAgentInput: Parameters<typeof ahsClient.editAgent>[0] | undefined

    getSlackGatewayBotStatusMock = async () => ({
      hasSlackBot: false,
    })
    requestSlackGatewayBotMock = async () => ({
      request_id: 'req-created',
      status: 'PENDING',
      message: 'queued',
    })
    getAgentMock = async () =>
      createAgentDetail({
        allowedGateways: [],
        creatorUserId: 'operator-1',
      })
    listAgentsMock = async () => {
      listAgentsCalls += 1
      throw new Error('listAgents should not be called for the owner')
    }
    editAgentMock = async (input) => {
      editAgentInput = input
      return createAgentDetail({
        allowedGateways: ['slack'],
      }) as never
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    await createCaller().installAgentBot({
      agentName: 'race-agent',
    })

    expect(listAgentsCalls).toBe(0)
    expect(editAgentInput).toEqual({
      name: 'race-agent',
      user_id: 'operator-1',
      allowed_gateways: ['slack'],
    })
  })

  it('returns the active request after the gateway reports a duplicate install', async () => {
    const statusCalls: Array<{
      agentName: string
      options?: { notFoundRetryDelaysMs?: readonly number[] }
    }> = []

    getSlackGatewayBotStatusMock = async (agentName, options) => {
      statusCalls.push({ agentName, options })

      if (statusCalls.length === 1) {
        return { hasSlackBot: false }
      }

      return {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-123',
          status: 'PENDING',
          displayName: 'Race Agent',
          createdAt: '2026-03-19T10:00:00Z',
          updatedAt: '2026-03-19T10:00:05Z',
        },
      }
    }

    requestSlackGatewayBotMock = async () => {
      throw new MockSlackAgentGatewayHttpError(
        409,
        'Slack agent gateway request failed.'
      )
    }

    getAgentMock = async () => createAgentDetail()
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called when Slack is enabled')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called when Slack is enabled')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    const result = await createCaller().installAgentBot({
      agentName: 'race-agent',
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-123',
        status: 'PENDING',
        displayName: 'Race Agent',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:05Z',
      },
    })

    expect(statusCalls).toEqual([
      {
        agentName: 'race-agent',
        options: undefined,
      },
      {
        agentName: 'race-agent',
        options: {
          notFoundRetryDelaysMs: [250, 500, 1_000, 1_500],
        },
      },
    ])
  })

  it('getBotStatus resolves the agent through AHS before reading gateway status', async () => {
    const calls: string[] = []
    const statusCalls: Array<{
      agentName: string
      options?: { notFoundRetryDelaysMs?: readonly number[] }
    }> = []

    getSlackGatewayBotStatusMock = async (agentName, options) => {
      calls.push('getSlackGatewayBotStatus')
      statusCalls.push({ agentName, options })

      return {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-999',
          status: 'PENDING',
          displayName: 'Race Agent',
          createdAt: '2026-03-19T10:00:00Z',
          updatedAt: '2026-03-19T10:00:05Z',
        },
      }
    }
    requestSlackGatewayBotMock = async () => {
      throw new Error('requestSlackGatewayBot should not be called')
    }
    getAgentMock = async () => {
      calls.push('getAgent')
      return createAgentDetail()
    }
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    const result = await createCaller().getBotStatus({
      agentName: 'race-agent',
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-999',
        status: 'PENDING',
        displayName: 'Race Agent',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:05Z',
      },
    })
    expect(calls).toEqual(['getAgent', 'getSlackGatewayBotStatus'])
    expect(statusCalls).toEqual([
      {
        agentName: 'race-agent',
        options: undefined,
      },
    ])
  })

  it('installAgentBot resolves the agent through AHS before reading gateway status', async () => {
    const calls: string[] = []
    const statusCalls: Array<{
      agentName: string
      options?: { notFoundRetryDelaysMs?: readonly number[] }
    }> = []

    getSlackGatewayBotStatusMock = async (agentName, options) => {
      calls.push('getSlackGatewayBotStatus')
      statusCalls.push({ agentName, options })

      return {
        hasSlackBot: false,
        pendingRequest: {
          requestId: 'req-999',
          status: 'PENDING',
          displayName: 'Race Agent',
          createdAt: '2026-03-19T10:00:00Z',
          updatedAt: '2026-03-19T10:00:05Z',
        },
      }
    }
    requestSlackGatewayBotMock = async () => {
      throw new Error('requestSlackGatewayBot should not be called')
    }
    getAgentMock = async () => {
      calls.push('getAgent')
      return createAgentDetail()
    }
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    const result = await createCaller().installAgentBot({
      agentName: 'race-agent',
    })

    expect(result).toEqual({
      hasSlackBot: false,
      pendingRequest: {
        requestId: 'req-999',
        status: 'PENDING',
        displayName: 'Race Agent',
        createdAt: '2026-03-19T10:00:00Z',
        updatedAt: '2026-03-19T10:00:05Z',
      },
    })
    expect(calls).toEqual(['getAgent', 'getSlackGatewayBotStatus'])
    expect(statusCalls).toEqual([
      {
        agentName: 'race-agent',
        options: undefined,
      },
    ])
  })

  it('stops before querying the gateway when the agent is missing in AHS', async () => {
    let gatewayCalls = 0

    getSlackGatewayBotStatusMock = async () => {
      gatewayCalls += 1
      return { hasSlackBot: false }
    }
    requestSlackGatewayBotMock = async () => {
      throw new Error('requestSlackGatewayBot should not be called')
    }
    getAgentMock = async () => {
      throw new MockAhsHttpError(404, 'AHS 404: secret backend detail')
    }
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    try {
      await createCaller().getBotStatus({
        agentName: 'missing-agent',
      })
      throw new Error(
        'Expected getBotStatus to fail when AHS cannot resolve the agent'
      )
    } catch (error) {
      expect(error).toMatchObject({
        code: 'NOT_FOUND',
        message: 'Agent not found.',
      })
    }

    expect(gatewayCalls).toBe(0)
  })

  it('does not query the gateway during install when the agent is missing in AHS', async () => {
    let gatewayCalls = 0

    getSlackGatewayBotStatusMock = async () => {
      gatewayCalls += 1
      return { hasSlackBot: false }
    }
    requestSlackGatewayBotMock = async () => {
      throw new Error('requestSlackGatewayBot should not be called')
    }
    getAgentMock = async () => {
      throw new MockAhsHttpError(404, 'AHS 404: secret backend detail')
    }
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    try {
      await createCaller().installAgentBot({
        agentName: 'missing-agent',
      })
      throw new Error(
        'Expected installAgentBot to fail when AHS cannot resolve the agent'
      )
    } catch (error) {
      expect(error).toMatchObject({
        code: 'NOT_FOUND',
        message: 'Agent not found.',
      })
    }

    expect(gatewayCalls).toBe(0)
  })

  it('returns an empty status and blocks installs when the gateway is not configured', async () => {
    getSlackGatewayBotStatusMock = async () => {
      throw new Error(
        'getSlackGatewayBotStatus should not be called when Slack is unavailable'
      )
    }
    requestSlackGatewayBotMock = async () => {
      throw new Error(
        'requestSlackGatewayBot should not be called when Slack is unavailable'
      )
    }
    getAgentMock = async () => {
      throw new Error('getAgent should not be called when Slack is unavailable')
    }
    listAgentsMock = async () => {
      throw new Error(
        'listAgents should not be called when Slack is unavailable'
      )
    }
    editAgentMock = async () => {
      throw new Error(
        'editAgent should not be called when Slack is unavailable'
      )
    }
    isSlackAgentGatewayConfiguredMock = () => false
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    expect(
      await createCaller().getBotStatus({
        agentName: 'race-agent',
      })
    ).toEqual({
      hasSlackBot: false,
    })

    try {
      await createCaller().installAgentBot({
        agentName: 'race-agent',
      })
      throw new Error(
        'Expected installAgentBot to fail when Slack is unavailable'
      )
    } catch (error) {
      expect(error).toMatchObject({
        code: 'PRECONDITION_FAILED',
        message:
          'Slack bot install is unavailable because this War Room environment is missing Slack gateway configuration.',
      })
    }
  })

  it('uses the owned-agent list fallback when creator_user_id is null', async () => {
    let capturedListAgentsInput:
      | Parameters<typeof ahsClient.listAgents>[0]
      | undefined
    let editAgentInput: Parameters<typeof ahsClient.editAgent>[0] | undefined

    getSlackGatewayBotStatusMock = async () => ({
      hasSlackBot: false,
    })
    requestSlackGatewayBotMock = async () => ({
      request_id: 'req-created',
      status: 'PENDING',
      message: 'queued',
    })
    getAgentMock = async () =>
      createAgentDetail({
        allowedGateways: [],
        creatorUserId: null,
      })
    listAgentsMock = async (input) => {
      capturedListAgentsInput = input
      return [createAgentDetail().agent] as never
    }
    editAgentMock = async (input) => {
      editAgentInput = input
      return createAgentDetail({
        allowedGateways: ['slack'],
      }) as never
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    await createCaller().installAgentBot({
      agentName: 'race-agent',
    })

    expect(capturedListAgentsInput).toEqual({
      include_all: false,
      user_id: 'operator-1',
    })
    expect(editAgentInput).toEqual({
      name: 'race-agent',
      user_id: 'operator-1',
      allowed_gateways: ['slack'],
    })
  })

  it('rejects shared or global agents when the owned-agent list fallback does not include them', async () => {
    let listAgentsCalls = 0

    getSlackGatewayBotStatusMock = async () => ({
      hasSlackBot: false,
    })
    requestSlackGatewayBotMock = async () => {
      throw new Error('requestSlackGatewayBot should not be called')
    }
    getAgentMock = async () =>
      createAgentDetail({
        allowedGateways: [],
        creatorUserId: null,
        displayName: 'Shared Agent',
        name: 'shared-agent',
      })
    listAgentsMock = async () => {
      listAgentsCalls += 1
      return []
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called for shared agents')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    try {
      await createCaller().installAgentBot({
        agentName: 'shared-agent',
      })
      throw new Error(
        'Expected installAgentBot to reject shared agent auto-enable'
      )
    } catch (error) {
      expect(error).toMatchObject({
        code: 'FORBIDDEN',
        message:
          'Slack install requires the Slack gateway, and this agent is read-only in War Room.',
      })
    }

    expect(listAgentsCalls).toBe(1)
  })

  it('sanitizes upstream failures before returning them to the client', async () => {
    getSlackGatewayBotStatusMock = async () => {
      throw new MockSlackAgentGatewayHttpError(
        500,
        'Slack agent gateway request failed.'
      )
    }
    requestSlackGatewayBotMock = async () => {
      throw new Error('requestSlackGatewayBot should not be called')
    }
    getAgentMock = async () => createAgentDetail()
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    try {
      await createCaller().getBotStatus({
        agentName: 'race-agent',
      })
      throw new Error('Expected getBotStatus to fail')
    } catch (error) {
      expect(error).toMatchObject({
        code: 'INTERNAL_SERVER_ERROR',
        message: 'Slack status is unavailable.',
      })
    }
  })

  it('uses the action-specific fallback message for non-Error throws', async () => {
    getSlackGatewayBotStatusMock = async () => {
      throw 'boom'
    }
    requestSlackGatewayBotMock = async () => {
      throw new Error('requestSlackGatewayBot should not be called')
    }
    getAgentMock = async () => createAgentDetail()
    listAgentsMock = async () => {
      throw new Error('listAgents should not be called')
    }
    editAgentMock = async () => {
      throw new Error('editAgent should not be called')
    }
    isSlackAgentGatewayConfiguredMock = () => true
    isAhsHttpErrorMock = ((error, status) =>
      error instanceof MockAhsHttpError &&
      (status === undefined ||
        error.status === status)) as typeof ahsClient.isAhsHttpError

    try {
      await createCaller().getBotStatus({
        agentName: 'race-agent',
      })
      throw new Error('Expected getBotStatus to fail')
    } catch (error) {
      expect(error).toMatchObject({
        code: 'INTERNAL_SERVER_ERROR',
        message: 'Slack status is unavailable.',
      })
    }
  })
})
