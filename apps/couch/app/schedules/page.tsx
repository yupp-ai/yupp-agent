import { Calendar } from 'lucide-react'
import { LeftRail } from '@/components/rail/left-rail'

export default function SchedulesPage() {
  return (
    <div className="flex h-screen">
      <LeftRail />
      <main className="flex flex-1 flex-col items-center justify-center text-muted-foreground">
        <Calendar className="mb-3 h-10 w-10 opacity-40" />
        <h1 className="font-semibold text-foreground text-xl">Schedules</h1>
        <p className="mt-1 max-w-md text-center text-sm">
          Pick a scheduled run from the left to view its session, or create
          new schedules from the war-room admin UI.
        </p>
      </main>
    </div>
  )
}
