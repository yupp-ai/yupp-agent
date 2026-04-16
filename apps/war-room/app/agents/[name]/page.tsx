import { Suspense } from 'react'
import { AgentDetail } from '@/components/agents/agent-detail'
import { Skeleton } from '@/components/ui/skeleton'
import { isSlackAgentGatewayConfigured } from '@/lib/slack-agent-gateway/server/client'

export default async function AgentDetailPage({
  params,
}: {
  params: Promise<{ name: string }>
}) {
  const { name } = await params
  const isSlackGatewayAvailable = isSlackAgentGatewayConfigured()

  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-5xl p-4">
          <div className="mb-6 space-y-3">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-8 w-56" />
            <Skeleton className="h-4 w-80" />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Skeleton className="h-64" />
            <Skeleton className="h-64" />
            <Skeleton className="h-48 lg:col-span-2" />
          </div>
        </div>
      }
    >
      <AgentDetail
        agentName={name}
        isSlackGatewayAvailable={isSlackGatewayAvailable}
      />
    </Suspense>
  )
}
