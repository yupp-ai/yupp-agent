import { cn } from '@/lib/utils'

export type SessionStatus = 'ACTIVE' | 'COMPLETED' | 'STALE' | string
export type WsStatus =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'error'

export interface StatusInputs {
  sessionStatus: SessionStatus
  wsStatus: WsStatus
  turnInFlight: boolean
}

export interface ResolvedStatus {
  label: string
  tone: 'idle' | 'working' | 'stopped' | 'reconnecting' | 'error'
}

export function resolveStatus(s: StatusInputs): ResolvedStatus {
  if (s.wsStatus === 'error') return { label: 'Error', tone: 'error' }
  if (s.wsStatus === 'reconnecting' || s.wsStatus === 'connecting') {
    return { label: 'Reconnecting…', tone: 'reconnecting' }
  }
  if (s.sessionStatus === 'COMPLETED' || s.sessionStatus === 'STALE') {
    return { label: 'Stopped', tone: 'stopped' }
  }
  if (s.turnInFlight) return { label: 'Working…', tone: 'working' }
  return { label: 'Couch is awaiting instructions', tone: 'idle' }
}

const TONE_CLASSES: Record<ResolvedStatus['tone'], string> = {
  idle: 'text-amber-600',
  working: 'text-blue-600',
  stopped: 'text-muted-foreground',
  reconnecting: 'text-amber-600',
  error: 'text-destructive',
}

export function StatusPill(props: StatusInputs) {
  const { label, tone } = resolveStatus(props)
  return (
    <div className={cn('flex items-center gap-2 text-sm', TONE_CLASSES[tone])}>
      <span
        aria-hidden
        className="inline-block h-1.5 w-1.5 rounded-full bg-current"
      />
      {label}
    </div>
  )
}
