import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'
import { SESSION_COOKIE_NAME } from '@/lib/auth/session-cookie-name'

// Routes that must remain reachable without auth. Anything else is
// redirected to /login (preserving the requested URL as ?redirectTo=).
const PUBLIC_PREFIXES = [
  '/login',
  '/api/authentication/',
  '/api/healthz',
  '/_next/',
  '/logo.png',
  '/favicon',
  '/icon',
]

function isPublic(pathname: string): boolean {
  if (PUBLIC_PREFIXES.some((p) => pathname === p || pathname.startsWith(p))) {
    return true
  }
  // Static files under /public also need to load on /login. Conservatively
  // allow anything with a file extension at the root.
  if (/^\/[^/]+\.[a-zA-Z0-9]+$/.test(pathname)) {
    return true
  }
  return false
}

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl

  // Dev bypass: NODE_ENV=development + COUCH_DEV_BYPASS_AUTH_EMAIL set.
  // Skip cookie check entirely so Playwright + local UI work without
  // a real Google login.
  if (
    process.env.NODE_ENV === 'development' &&
    process.env.COUCH_DEV_BYPASS_AUTH_EMAIL
  ) {
    return NextResponse.next()
  }

  if (isPublic(pathname)) {
    return NextResponse.next()
  }

  const cookie = req.cookies.get(SESSION_COOKIE_NAME)?.value
  if (cookie) {
    // Trust presence here; full signature/expiry validation happens
    // server-side via getSession() in pages and route handlers.
    return NextResponse.next()
  }

  const loginUrl = new URL('/login', req.url)
  if (pathname !== '/') {
    loginUrl.searchParams.set(
      'redirectTo',
      `${pathname}${req.nextUrl.search}`
    )
  }
  return NextResponse.redirect(loginUrl)
}

// Run on everything except Next internals + static assets.
export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
}
