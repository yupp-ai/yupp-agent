import type { AhsAgentInfo } from '@/lib/ahs/types'

type AgentOwnershipArgs = {
  agentName: string
  creatorUserId?: string | null
  currentUserId: string
  userAgents?: ReadonlyArray<Pick<AhsAgentInfo, 'name'>>
}

export function isAgentOwnedByCurrentUser({
  agentName,
  creatorUserId,
  currentUserId,
  userAgents,
}: AgentOwnershipArgs): boolean {
  if (creatorUserId === currentUserId) {
    return true
  }

  return userAgents?.some((agent) => agent.name === agentName) ?? false
}

export function canCurrentUserManageAgent({
  agentName,
  creatorUserId,
  currentUserId,
  userAgents,
}: AgentOwnershipArgs): boolean {
  return isAgentOwnedByCurrentUser({
    agentName,
    creatorUserId,
    currentUserId,
    userAgents,
  })
}
