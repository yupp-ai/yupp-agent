import { NewSessionPrompt } from '@/components/prompt/new-session-prompt'
import { LeftRail } from '@/components/rail/left-rail'

export default function Home() {
  return (
    <div className="flex h-screen">
      <LeftRail />
      <main className="flex flex-1 flex-col">
        <NewSessionPrompt />
      </main>
    </div>
  )
}
