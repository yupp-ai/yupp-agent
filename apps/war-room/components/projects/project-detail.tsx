'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { type KeyboardEvent, useState } from 'react'
import { ProjectBoard } from '@/components/projects/project-board'
import { ProjectTaskDialog } from '@/components/projects/project-task-dialog'
import {
  dependencyLabel,
  formatDateTime,
  formatMoney,
  priorityBadgeClass,
  projectStatusBadgeClass,
  taskStatusBadgeClass,
} from '@/components/projects/project-utils'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { AhsTaskResponse, AhsTaskStatusValue } from '@/lib/ahs/types'
import { AHS_TASK_STATUS_VALUES } from '@/lib/ahs/types'
import { useTRPC } from '@/lib/hooks/trpc-client'

type ProjectDetailProps = {
  projectId: string
}

const TASK_STATUS_ORDER: AhsTaskStatusValue[] = [...AHS_TASK_STATUS_VALUES]

function handleTaskCardKeyDown(
  event: KeyboardEvent<HTMLDivElement>,
  onViewTask: (task: AhsTaskResponse) => void,
  task: AhsTaskResponse
) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    onViewTask(task)
  }
}

function TaskRow({
  onViewTask,
  task,
}: {
  onViewTask: (task: AhsTaskResponse) => void
  task: AhsTaskResponse
}) {
  const dependencyText = dependencyLabel(task)

  return (
    <Card
      className="cursor-pointer transition hover:ring-foreground/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-500"
      onClick={() => onViewTask(task)}
      onKeyDown={(event) => handleTaskCardKeyDown(event, onViewTask, task)}
      role="button"
      size="sm"
      tabIndex={0}
    >
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <CardTitle>{task.title}</CardTitle>
          <div className="flex flex-wrap gap-2">
            <Badge className={taskStatusBadgeClass(task.status)}>
              {task.status}
            </Badge>
            <Badge className={priorityBadgeClass(task.priority)}>
              {task.priority}
            </Badge>
          </div>
        </div>
        {task.description && (
          <CardDescription>{task.description}</CardDescription>
        )}
      </CardHeader>
      <CardContent className="space-y-2 text-sm text-zinc-500">
        <p>{task.agent_name ?? 'Unassigned'}</p>
        {dependencyText && <p>{dependencyText}</p>}
        <p>Created: {formatDateTime(task.created_at)}</p>
        {task.completed_at && (
          <p>Completed: {formatDateTime(task.completed_at)}</p>
        )}
      </CardContent>
    </Card>
  )
}

function SummaryField({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">
        {label}
      </p>
      <p className="text-sm text-zinc-100">{value}</p>
    </div>
  )
}

export function ProjectDetail({ projectId }: ProjectDetailProps) {
  const trpc = useTRPC()
  const [selectedTask, setSelectedTask] = useState<AhsTaskResponse | null>(null)
  const {
    data: project,
    error: projectError,
    isLoading: isProjectLoading,
  } = useQuery(trpc.ahs.getProject.queryOptions({ projectId }))
  const {
    data: tasksData,
    error: tasksError,
    isLoading: isTasksLoading,
  } = useQuery(
    trpc.ahs.listProjectTasks.queryOptions({
      projectId,
      limit: 500,
      offset: 0,
    })
  )

  const tasks = tasksData?.items ?? []
  const loading = isProjectLoading || isTasksLoading
  const error = projectError ?? tasksError

  if (loading) {
    return (
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
    )
  }

  if (error || !project) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <Link
          className="text-sm text-zinc-500 transition-colors hover:text-zinc-200"
          href="/projects"
        >
          ← Back to projects
        </Link>
        <p className="mt-6 text-sm text-red-400">
          {error?.message ?? 'Project not found.'}
        </p>
      </div>
    )
  }

  const taskSummary = project.task_summary

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <Link
            className="text-sm text-zinc-500 transition-colors hover:text-zinc-200"
            href="/projects"
          >
            ← Back to projects
          </Link>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="font-bold text-2xl tracking-tight text-zinc-50">
              {project.name}
            </h1>
            <Badge className={projectStatusBadgeClass(project.status)}>
              {project.status}
            </Badge>
          </div>
          <p className="max-w-3xl text-sm text-zinc-300">
            {project.description ?? 'No description provided.'}
          </p>
        </div>
      </div>

      <Card className="mb-4">
        <CardHeader>
          <CardTitle>Project summary</CardTitle>
          <CardDescription>Metadata returned from AHS.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <SummaryField
            label="Creator"
            value={project.creator_user_name ?? project.creator_user_id ?? '—'}
          />
          <SummaryField label="Slack" value={project.slack_channel ?? '—'} />
          <SummaryField
            label="Created"
            value={formatDateTime(project.created_at)}
          />
          <SummaryField
            label="Budget"
            value={formatMoney(project.budget_usd)}
          />
          <SummaryField
            label="Spent"
            value={formatMoney(project.budget_spent_usd)}
          />
          <SummaryField label="Task total" value={`${taskSummary.total}`} />
        </CardContent>
      </Card>

      <Card className="mb-4">
        <CardHeader>
          <CardTitle>Task summary</CardTitle>
          <CardDescription>Counts by exact AHS status.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {TASK_STATUS_ORDER.filter((status) => taskSummary[status] > 0).map(
            (status) => (
              <Badge className={taskStatusBadgeClass(status)} key={status}>
                {status} {taskSummary[status]}
              </Badge>
            )
          )}
          <Badge className="bg-zinc-800 text-zinc-200">
            Total {taskSummary.total}
          </Badge>
        </CardContent>
      </Card>

      {tasksData && tasksData.total > tasks.length && (
        <p className="mb-4 text-sm text-zinc-400">
          Showing the first {tasks.length} of {tasksData.total} tasks.
        </p>
      )}

      <Tabs className="w-full" defaultValue="board">
        <TabsList variant="line" className="mb-4">
          <TabsTrigger value="board">Board</TabsTrigger>
          <TabsTrigger value="overview">Overview</TabsTrigger>
        </TabsList>

        <TabsContent value="board">
          <ProjectBoard onViewTask={setSelectedTask} tasks={tasks} />
        </TabsContent>

        <TabsContent value="overview">
          <div className="space-y-4">
            {tasks.length === 0 ? (
              <p className="text-sm text-zinc-400">No tasks found.</p>
            ) : (
              tasks.map((task) => (
                <TaskRow
                  key={task.agent_task_id}
                  onViewTask={setSelectedTask}
                  task={task}
                />
              ))
            )}
          </div>
        </TabsContent>
      </Tabs>

      {selectedTask && (
        <ProjectTaskDialog
          initialTask={selectedTask}
          key={selectedTask.agent_task_id}
          onOpenChange={(open) => {
            if (!open) {
              setSelectedTask(null)
            }
          }}
          open
          projectId={projectId}
        />
      )}
    </div>
  )
}
