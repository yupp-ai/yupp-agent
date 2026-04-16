import { notFound } from 'next/navigation'
import { SideBySideSessionLayout } from '@/components/sessions/side-by-side-session-layout'
import { getSideBySideSessionIds } from '@/lib/ahs/session-route-params'

export default async function SideBySideSessionsPage({
  params,
}: {
  params: Promise<{ left: string; right: string }>
}) {
  const sessionIds = getSideBySideSessionIds(await params)

  if (!sessionIds) {
    notFound()
  }

  return (
    <SideBySideSessionLayout
      leftSessionId={sessionIds.left}
      rightSessionId={sessionIds.right}
    />
  )
}
