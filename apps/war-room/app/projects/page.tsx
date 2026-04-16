'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  formatDateTime,
  projectStatusBadgeClass,
  taskStatusBadgeClass,
} from '@/components/projects/project-utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  AHS_PROJECT_STATUS_VALUES,
  AHS_TASK_STATUS_VALUES,
  type AhsProjectResponse,
  type AhsProjectStatusValue,
} from '@/lib/ahs/types'
import { useTRPC } from '@/lib/hooks/trpc-client'

const PAGE_SIZE = 20

const STATUS_OPTIONS: Array<{
  label: string
  value: AhsProjectStatusValue | ''
}> = [
  { label: 'All statuses', value: '' },
  ...AHS_PROJECT_STATUS_VALUES.map((value) => ({ label: value, value })),
]

function parseProjectStatusFilter(value: string): AhsProjectStatusValue | '' {
  if (value === '') {
    return ''
  }

  return AHS_PROJECT_STATUS_VALUES.find((status) => status === value) ?? ''
}

function ProjectTaskSummary({ project }: { project: AhsProjectResponse }) {
  const taskSummary = project.task_summary
  const populatedStatuses = AHS_TASK_STATUS_VALUES.filter(
    (status) => taskSummary[status] > 0
  )

  return (
    <div className="flex flex-wrap gap-2">
      {populatedStatuses.map((status) => (
        <Badge className={taskStatusBadgeClass(status)} key={status}>
          {status} {taskSummary[status]}
        </Badge>
      ))}
      <Badge className="bg-zinc-800 text-zinc-200">
        Total {taskSummary.total}
      </Badge>
    </div>
  )
}

function ProjectCard({ project }: { project: AhsProjectResponse }) {
  return (
    <Link
      className="block h-full"
      href={`/projects/${encodeURIComponent(project.agent_project_id)}`}
    >
      <Card className="h-full transition-colors hover:bg-zinc-900/80 hover:ring-foreground/20">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-2">
              <CardTitle>{project.name}</CardTitle>
              <CardDescription className="line-clamp-2">
                {project.description ?? 'No description provided.'}
              </CardDescription>
            </div>
            <Badge className={projectStatusBadgeClass(project.status)}>
              {project.status}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <ProjectTaskSummary project={project} />
        </CardContent>
        <CardFooter className="flex flex-wrap justify-between gap-3 text-xs text-zinc-500">
          <span>{project.creator_user_name ?? 'Unknown creator'}</span>
          <span>{formatDateTime(project.created_at)}</span>
        </CardFooter>
      </Card>
    </Link>
  )
}

export default function ProjectsPage() {
  const trpc = useTRPC()
  const [statusFilter, setStatusFilter] = useState<AhsProjectStatusValue | ''>(
    ''
  )
  const [mineOnly, setMineOnly] = useState(true)
  const [offset, setOffset] = useState(0)
  const [accumulated, setAccumulated] = useState<AhsProjectResponse[]>([])
  const paginationLockRef = useRef(false)

  const queryInput = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset,
      mine_only: mineOnly,
      ...(statusFilter ? { status: statusFilter } : {}),
    }),
    [mineOnly, offset, statusFilter]
  )

  const { data, error, isFetching, isLoading } = useQuery({
    ...trpc.ahs.listProjects.queryOptions(queryInput),
    placeholderData: (previous) => previous,
  })

  const projects = useMemo(() => {
    if (!data) return accumulated
    if (offset === 0) return data.items

    const seen = new Set(accumulated.map((project) => project.agent_project_id))
    const nextProjects = data.items.filter(
      (project) => !seen.has(project.agent_project_id)
    )

    return [...accumulated, ...nextProjects]
  }, [accumulated, data, offset])

  const total = data?.total ?? 0

  useEffect(() => {
    if (!isFetching) {
      paginationLockRef.current = false
    }
  }, [isFetching])

  const resetPagination = useCallback(() => {
    setOffset(0)
    setAccumulated([])
  }, [])

  const loadMore = useCallback(() => {
    if (
      isFetching ||
      paginationLockRef.current ||
      projects.length >= total ||
      total === 0
    ) {
      return
    }

    paginationLockRef.current = true
    setAccumulated(projects)
    setOffset((current) => current + PAGE_SIZE)
  }, [isFetching, projects, total])

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-6">
        <h1 className="font-bold text-lg tracking-tight">Projects</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Browse AHS projects, then open one to inspect its tasks and board.
        </p>
      </div>

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Label size="sm">
          Status
          <Select
            className="px-2 py-1.5"
            onValueChange={(value) => {
              setStatusFilter(parseProjectStatusFilter(value))
              resetPagination()
            }}
            options={STATUS_OPTIONS}
            value={statusFilter}
          />
        </Label>

        <Label
          className="flex-row items-center gap-3 self-end text-sm text-zinc-300"
          htmlFor="projects-mine-only"
          size="sm"
        >
          <Switch
            checked={mineOnly}
            id="projects-mine-only"
            onCheckedChange={(checked) => {
              setMineOnly(checked)
              resetPagination()
            }}
          />
          Mine only
        </Label>
      </div>

      {error && <p className="mb-4 text-sm text-red-400">{error.message}</p>}

      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[1, 2, 3, 4, 5, 6].map((key) => (
            <Card key={key}>
              <CardHeader className="space-y-3">
                <Skeleton className="h-5 w-32" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-2/3" />
              </CardHeader>
              <CardContent className="space-y-2">
                <div className="flex flex-wrap gap-2">
                  <Skeleton className="h-5 w-16 rounded-full" />
                  <Skeleton className="h-5 w-24 rounded-full" />
                  <Skeleton className="h-5 w-20 rounded-full" />
                </div>
              </CardContent>
              <CardFooter className="flex justify-between gap-3">
                <Skeleton className="h-3 w-28" />
                <Skeleton className="h-3 w-32" />
              </CardFooter>
            </Card>
          ))}
        </div>
      )}

      {!isLoading && !error && projects.length === 0 && (
        <p className="text-sm text-zinc-400">No projects found.</p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {projects.map((project) => (
          <ProjectCard key={project.agent_project_id} project={project} />
        ))}
      </div>

      {projects.length < total && (
        <div className="mt-4 flex justify-center">
          <Button disabled={isFetching} onClick={loadMore} type="button">
            {isFetching ? 'Loading...' : 'Load more'}
          </Button>
        </div>
      )}
    </div>
  )
}
