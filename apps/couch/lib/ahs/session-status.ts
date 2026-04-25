import type { AhsSessionStatus } from './types'

export function statusColor(status: AhsSessionStatus): string {
  switch (status) {
    case 'ACTIVE':
      return 'bg-emerald-400'
    case 'COMPLETED':
      return 'bg-zinc-400'
    case 'STALE':
      return 'bg-amber-400'
    default:
      return 'bg-zinc-500'
  }
}
