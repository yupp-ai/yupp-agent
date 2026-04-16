import { useEffect, useState } from 'react'
import type { AhsAgentInfo } from '@/lib/ahs/types'

const SELECTED_AGENT_STORAGE_KEY = 'war-room:selected-agent'

function readStoredAgent() {
  if (typeof window === 'undefined' || !window.localStorage) {
    return ''
  }

  try {
    return window.localStorage.getItem(SELECTED_AGENT_STORAGE_KEY) ?? ''
  } catch {
    // Ignore storage access failures, such as private browsing restrictions.
    return ''
  }
}

function writeStoredAgent(agentName: string) {
  if (typeof window === 'undefined' || !window.localStorage) {
    return
  }

  try {
    window.localStorage.setItem(SELECTED_AGENT_STORAGE_KEY, agentName)
  } catch {
    // Ignore storage access failures, such as private browsing restrictions.
  }
}

export function persistSelectedAgent(agentName: string) {
  writeStoredAgent(agentName)
}

function clearStoredAgent() {
  if (typeof window === 'undefined' || !window.localStorage) {
    return
  }

  try {
    window.localStorage.removeItem(SELECTED_AGENT_STORAGE_KEY)
  } catch {
    // Ignore storage access failures, such as private browsing restrictions.
  }
}

export function usePersistedAgentSelection(
  agents: Pick<AhsAgentInfo, 'name'>[],
  canValidateSelection: boolean
) {
  const [selectedAgent, setSelectedAgentState] = useState<string | null>(null)
  const fallbackAgent = agents[0]?.name ?? ''
  const hasSelectedAgent =
    selectedAgent !== null &&
    agents.some((agent) => agent.name === selectedAgent)
  const resolvedAgent = hasSelectedAgent ? selectedAgent : fallbackAgent

  useEffect(() => {
    setSelectedAgentState(readStoredAgent())
  }, [])

  useEffect(() => {
    if (selectedAgent === null || !canValidateSelection) {
      return
    }

    if (!hasSelectedAgent && selectedAgent !== fallbackAgent) {
      setSelectedAgentState(fallbackAgent)
    }
  }, [canValidateSelection, fallbackAgent, hasSelectedAgent, selectedAgent])

  useEffect(() => {
    if (selectedAgent === null || !canValidateSelection) {
      return
    }

    if (hasSelectedAgent) {
      persistSelectedAgent(selectedAgent)
      return
    }

    clearStoredAgent()
  }, [canValidateSelection, hasSelectedAgent, selectedAgent])

  return {
    resolvedAgent,
    setSelectedAgent: setSelectedAgentState,
  }
}
