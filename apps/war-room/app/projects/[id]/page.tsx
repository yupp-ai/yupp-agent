import { Suspense } from 'react'
import { ProjectDetail } from '@/components/projects/project-detail'
import { Skeleton } from '@/components/ui/skeleton'

export default async function ProjectDetailPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params

  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-5xl p-4">
          <div className="mb-6 space-y-3">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-8 w-64" />
            <Skeleton className="h-4 w-96" />
          </div>
          <div className="space-y-4">
            <Skeleton className="h-52 w-full" />
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-80 w-full" />
          </div>
        </div>
      }
    >
      <ProjectDetail projectId={id} />
    </Suspense>
  )
}
