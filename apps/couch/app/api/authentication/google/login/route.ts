import { type NextRequest, NextResponse } from 'next/server'
import { GOOGLE_CSRF_COOKIE_NAME, initOauthState } from '@/lib/auth/oauth-utils'
import { getGoogleAuthUrl } from '@/lib/auth/vanilla-google-oauth-provider'

export async function GET(request: NextRequest) {
  try {
    const oauthState = await initOauthState(
      request,
      GOOGLE_CSRF_COOKIE_NAME,
      '/api/authentication/google/callback'
    )

    return NextResponse.redirect(
      new URL(
        getGoogleAuthUrl({
          state: oauthState,
        })
      )
    )
  } catch {
    return NextResponse.redirect(new URL('/?error=authentication', request.url))
  }
}

export async function POST(request: NextRequest) {
  try {
    const oauthState = await initOauthState(
      request,
      GOOGLE_CSRF_COOKIE_NAME,
      '/api/authentication/google/callback'
    )

    return NextResponse.json({
      redirectUrl: getGoogleAuthUrl({
        state: oauthState,
      }),
    })
  } catch {
    return NextResponse.json(
      {
        error: 'Internal Server Error',
      },
      { status: 500 }
    )
  }
}
