import type {
  AhsProjectStatusValue,
  AhsTaskResponse,
  AhsTaskStatusValue,
} from '@/lib/ahs/types'

export function formatDateTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : '—'
}

export function formatMoney(value: number | string | null | undefined): string {
  if (value == null || value === '') {
    return '—'
  }

  const numericValue = Number(value)
  if (!Number.isFinite(numericValue)) {
    return '—'
  }

  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 2,
  }).format(numericValue)
}

export function projectStatusBadgeClass(status: AhsProjectStatusValue): string {
  switch (status) {
    case 'ACTIVE':
      return 'bg-emerald-900/40 text-emerald-100'
    case 'PAUSED':
      return 'bg-amber-900/40 text-amber-100'
    case 'COMPLETED':
      return 'bg-blue-900/40 text-blue-100'
    case 'ARCHIVED':
      return 'bg-zinc-800 text-zinc-300'
  }
}

export function taskStatusBadgeClass(status: AhsTaskStatusValue): string {
  switch (status) {
    case 'PENDING':
      return 'bg-amber-900/40 text-amber-100'
    case 'BLOCKED':
      return 'bg-red-900/40 text-red-100'
    case 'READY':
      return 'bg-blue-900/40 text-blue-100'
    case 'IN_PROGRESS':
      return 'bg-cyan-900/40 text-cyan-100'
    case 'IN_REVIEW':
      return 'bg-violet-900/40 text-violet-100'
    case 'COMPLETED':
      return 'bg-emerald-900/40 text-emerald-100'
    case 'FAILED':
      return 'bg-rose-900/40 text-rose-100'
    case 'CANCELLED':
      return 'bg-zinc-800 text-zinc-300'
  }
}

export function priorityBadgeClass(
  priority: AhsTaskResponse['priority']
): string {
  switch (priority) {
    case 'URGENT':
      return 'bg-rose-900/40 text-rose-100'
    case 'HIGH':
      return 'bg-orange-900/40 text-orange-100'
    case 'NORMAL':
      return 'bg-zinc-800 text-zinc-300'
    case 'LOW':
      return 'bg-emerald-900/40 text-emerald-100'
    default:
      return 'bg-zinc-800 text-zinc-300'
  }
}

export function dependencyLabel(task: AhsTaskResponse): string | null {
  const dependencyCount = task.depends_on?.length ?? 0

  if (dependencyCount === 0) {
    return null
  }

  return dependencyCount === 1
    ? '1 dependency'
    : `${dependencyCount} dependencies`
}
