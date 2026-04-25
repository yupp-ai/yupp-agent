import { notFound } from 'next/navigation'
import { getSessionAction } from '@/app/_actions/get-session'
import { LeftRail } from '@/components/rail/left-rail'
import { SessionView } from '@/components/session/session-view'
import { getSessionDisplayTitle } from '@/lib/ahs/session-title'

export default async function SessionPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params
  let detail
  try {
    detail = await getSessionAction(id)
  } catch {
    notFound()
  }

  if (!detail) notFound()

  const title = getSessionDisplayTitle(detail.session)

  return (
    <div className="flex h-screen">
      <LeftRail activeSessionId={id} />
      <SessionView
        initialStatus={detail.session.status}
        initialTitle={title}
        sessionId={id}
      />
    </div>
  )
}
