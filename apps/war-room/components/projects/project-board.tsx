import type { KeyboardEvent } from 'react'
import {
  dependencyLabel,
  priorityBadgeClass,
} from '@/components/projects/project-utils'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  AHS_TASK_STATUS_VALUES,
  type AhsTaskResponse,
  type AhsTaskStatusValue,
} from '@/lib/ahs/types'

const BOARD_COLUMNS: AhsTaskStatusValue[] = [...AHS_TASK_STATUS_VALUES]

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

export function ProjectBoard({
  onViewTask,
  tasks,
}: {
  onViewTask: (task: AhsTaskResponse) => void
  tasks: AhsTaskResponse[]
}) {
  const groupedTasks = BOARD_COLUMNS.map((status) => ({
    status,
    tasks: tasks.filter((task) => task.status === status),
  })).filter((column) => column.tasks.length > 0)

  if (groupedTasks.length === 0) {
    return <p className="text-sm text-zinc-400">No tasks found.</p>
  }

  return (
    <div className="overflow-x-auto px-1 py-1">
      <div className="flex min-w-max gap-4 pb-1">
        {groupedTasks.map((column) => (
          <div className="w-80 shrink-0" key={column.status}>
            <Card className="h-full bg-zinc-950/70">
              <CardHeader className="border-b border-zinc-800/80">
                <div className="flex items-center justify-between gap-3">
                  <CardTitle>{column.status}</CardTitle>
                  <Badge className="bg-zinc-800 text-zinc-300">
                    {column.tasks.length}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-3 pt-4">
                {column.tasks.map((task) => {
                  const dependencyText = dependencyLabel(task)

                  return (
                    <Card
                      className="cursor-pointer border border-zinc-800/80 bg-zinc-950/60 shadow-none transition hover:ring-foreground/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-500"
                      key={task.agent_task_id}
                      onClick={() => onViewTask(task)}
                      onKeyDown={(event) =>
                        handleTaskCardKeyDown(event, onViewTask, task)
                      }
                      role="button"
                      size="sm"
                      tabIndex={0}
                    >
                      <CardHeader className="px-4 pt-4">
                        <CardTitle>{task.title}</CardTitle>
                      </CardHeader>
                      <CardContent className="space-y-2 px-4 pb-4 text-sm text-zinc-500">
                        <div className="flex flex-wrap gap-2">
                          <Badge className={priorityBadgeClass(task.priority)}>
                            {task.priority}
                          </Badge>
                        </div>
                        {task.description && (
                          <p className="line-clamp-2 text-zinc-400">
                            {task.description}
                          </p>
                        )}
                        <p>{task.agent_name ?? 'Unassigned'}</p>
                        {dependencyText && <p>{dependencyText}</p>}
                      </CardContent>
                    </Card>
                  )
                })}
              </CardContent>
            </Card>
          </div>
        ))}
      </div>
    </div>
  )
}
