import { describe, expect, it } from 'bun:test'
import {
  canCurrentUserManageAgent,
  isAgentOwnedByCurrentUser,
} from './ownership'

describe('isAgentOwnedByCurrentUser', () => {
  it('returns true when the backend reports the current user as creator', () => {
    expect(
      isAgentOwnedByCurrentUser({
        agentName: 'hercule-poirot',
        creatorUserId: 'user-123',
        currentUserId: 'user-123',
      })
    ).toBe(true)
  })

  it('returns true when the agent appears in the current user list', () => {
    expect(
      isAgentOwnedByCurrentUser({
        agentName: 'hercule-poirot',
        creatorUserId: null,
        currentUserId: 'user-123',
        userAgents: [{ name: 'hercule-poirot' }],
      })
    ).toBe(true)
  })

  it('prefers the user list fallback when creator_user_id is stale', () => {
    expect(
      isAgentOwnedByCurrentUser({
        agentName: 'hercule-poirot',
        creatorUserId: 'legacy-user-id',
        currentUserId: 'user-123',
        userAgents: [{ name: 'hercule-poirot' }],
      })
    ).toBe(true)
  })

  it('returns false when neither ownership signal matches', () => {
    expect(
      isAgentOwnedByCurrentUser({
        agentName: 'hercule-poirot',
        creatorUserId: null,
        currentUserId: 'user-123',
        userAgents: [{ name: 'reviewer' }],
      })
    ).toBe(false)
  })
})

describe('canCurrentUserManageAgent', () => {
  it('returns true when the backend reports the current user as creator', () => {
    expect(
      canCurrentUserManageAgent({
        agentName: 'hercule-poirot',
        creatorUserId: 'user-123',
        currentUserId: 'user-123',
      })
    ).toBe(true)
  })

  it('falls back to the user list when creator_user_id is omitted', () => {
    expect(
      canCurrentUserManageAgent({
        agentName: 'hercule-poirot',
        currentUserId: 'user-123',
        userAgents: [{ name: 'hercule-poirot' }],
      })
    ).toBe(true)
  })

  it('returns true when a stale creator id falls back to the user list', () => {
    expect(
      canCurrentUserManageAgent({
        agentName: 'hercule-poirot',
        creatorUserId: 'legacy-user-id',
        currentUserId: 'user-123',
        userAgents: [{ name: 'hercule-poirot' }],
      })
    ).toBe(true)
  })

  it('treats the owned-agent list as authoritative even when creator_user_id is null', () => {
    expect(
      canCurrentUserManageAgent({
        agentName: 'hercule-poirot',
        creatorUserId: null,
        currentUserId: 'user-123',
        userAgents: [{ name: 'hercule-poirot' }],
      })
    ).toBe(true)
  })

  it('returns false when neither creator id nor the owned-agent list matches', () => {
    expect(
      canCurrentUserManageAgent({
        agentName: 'hercule-poirot',
        creatorUserId: null,
        currentUserId: 'user-123',
        userAgents: [{ name: 'reviewer' }],
      })
    ).toBe(false)
  })
})
