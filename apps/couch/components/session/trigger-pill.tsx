import { cn } from '@/lib/utils'
import type { AhsTriggerType } from '@/lib/ahs/types'

const LABEL: Record<AhsTriggerType, string> = {
  SLACK: 'Slack',
  API: 'API',
  CRON: 'Cron',
  WEBHOOK: 'Webhook',
  TASK: 'Task',
}

const TONE: Record<AhsTriggerType, string> = {
  SLACK: 'border-purple-300/60 bg-purple-100 text-purple-800',
  API: 'border-sky-300/60 bg-sky-100 text-sky-800',
  CRON: 'border-amber-300/60 bg-amber-100 text-amber-800',
  WEBHOOK: 'border-emerald-300/60 bg-emerald-100 text-emerald-800',
  TASK: 'border-zinc-300/60 bg-zinc-100 text-zinc-800',
}

export function TriggerPill({
  trigger,
  className,
}: {
  trigger: AhsTriggerType | string | null | undefined
  className?: string
}) {
  if (!trigger) return null
  const key = trigger.toUpperCase() as AhsTriggerType
  const label = LABEL[key] ?? trigger
  const tone = TONE[key] ?? TONE.TASK
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 font-medium text-xs',
        tone,
        className
      )}
    >
      {label}
    </span>
  )
}
