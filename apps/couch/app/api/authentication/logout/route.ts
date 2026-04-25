import { type NextRequest, NextResponse } from 'next/server'
import { deleteSessionCookie } from '@/lib/auth/session-cookie'

async function handleLogout(request: NextRequest) {
  const response = NextResponse.redirect(new URL('/', request.url), {
    status: 303,
  })
  deleteSessionCookie(response.cookies, request.nextUrl.hostname)
  return response
}

export const GET = handleLogout
export const POST = handleLogout
