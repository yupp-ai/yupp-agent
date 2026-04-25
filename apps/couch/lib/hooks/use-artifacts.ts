'use client'

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { listArtifactsAction } from '@/app/_actions/list-artifacts'
import { resolveArtifactRefetchInterval } from './artifact-refresh'

export { resolveArtifactRefetchInterval } from './artifact-refresh'

export function useArtifacts({
  sessionId,
  turnInFlight,
  itemCount,
}: {
  sessionId: string
  turnInFlight: boolean
  itemCount: number
}) {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: ['artifacts', sessionId],
    queryFn: () => listArtifactsAction(sessionId),
    refetchInterval: resolveArtifactRefetchInterval(turnInFlight),
  })

  useEffect(() => {
    qc.invalidateQueries({ queryKey: ['artifacts', sessionId] })
  }, [itemCount, qc, sessionId])

  return query
}
