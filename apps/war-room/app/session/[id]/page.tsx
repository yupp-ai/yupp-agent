import { notFound } from 'next/navigation'
import { Suspense } from 'react'
import { SessionViewer } from '@/components/sessions/session-viewer'
import { Skeleton } from '@/components/ui/skeleton'
import { isSessionRouteIdValid } from '@/lib/ahs/session-route-params'

export default async function SessionPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params

  if (!isSessionRouteIdValid(id)) {
    notFound()
  }

  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-3xl p-4">
          <div className="mb-6 flex items-center gap-3">
            <Skeleton className="h-2.5 w-2.5 rounded-full" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-3 w-64" />
            </div>
            <Skeleton className="h-5 w-20 rounded-full" />
          </div>
          <div className="flex">
            <Skeleton className="ml-auto h-12 w-56 rounded-xl" />
          </div>
        </div>
      }
    >
      <SessionViewer layout="page" sessionId={id} />
    </Suspense>
  )
}
