import { NextResponse } from 'next/server'
import { readArtifactContent } from '@/lib/ahs/server/client'
import { getInternalSession } from '@/lib/auth/get-session'

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const auth = await getInternalSession()
  if (auth.status === 'unauthenticated') {
    return new NextResponse('Unauthorized', { status: 401 })
  }
  const { id } = await params
  const upstream = await readArtifactContent(id)
  if (upstream.status >= 400) {
    return new NextResponse(`Upstream ${upstream.status}`, {
      status: upstream.status,
    })
  }
  const headers: Record<string, string> = {
    'Content-Type': upstream.contentType,
  }
  if (upstream.contentDisposition) {
    headers['Content-Disposition'] = upstream.contentDisposition
  }
  return new NextResponse(upstream.body, { status: 200, headers })
}
