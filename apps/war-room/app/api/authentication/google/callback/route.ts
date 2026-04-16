import { unstable_rethrow } from 'next/navigation'
import { type NextRequest, NextResponse } from 'next/server'
import { isAhsHttpError, resolveUserByEmail } from '@/lib/ahs/server/client'
import { canAccessWarRoomDuringLogin } from '@/lib/auth/authorization'
import {
  decodeAndValidateOauthState,
  GOOGLE_CSRF_COOKIE_NAME,
} from '@/lib/auth/oauth-utils'
import {
  createCookieInfo,
  createSessionCookiePayload,
} from '@/lib/auth/session-cookie'
import { exchangeCodeForIdToken } from '@/lib/auth/vanilla-google-oauth-provider'

function getGoogleFirstName(input: {
  given_name?: string
  name?: string
}): string | undefined {
  const givenName = input.given_name?.trim()
  if (givenName) {
    return givenName
  }

  const fullName = input.name?.trim()
  if (!fullName) {
    return undefined
  }

  const [firstName] = fullName.split(/\s+/)
  return firstName || undefined
}

function redirectToError(request: NextRequest, redirectTo?: string) {
  const url = new URL(redirectTo || '/', request.url)
  url.searchParams.set('error', 'authentication')
  return NextResponse.redirect(url)
}

function redirectToUnauthorized(request: NextRequest) {
  return NextResponse.redirect(new URL('/?error=unauthorized', request.url))
}

export async function GET(request: NextRequest) {
  try {
    const errorFromGoogle = request.nextUrl.searchParams.get('error')
    if (errorFromGoogle) {
      return redirectToError(request)
    }

    const code = request.nextUrl.searchParams.get('code')
    const encodedState = request.nextUrl.searchParams.get('state')

    if (!encodedState) {
      return redirectToError(request)
    }

    const oauthState = decodeAndValidateOauthState(
      encodedState,
      request,
      GOOGLE_CSRF_COOKIE_NAME,
      '/api/authentication/google/callback'
    )

    if (!oauthState.isValid || !code) {
      return redirectToError(request, oauthState.redirectTo)
    }

    const userInfo = await exchangeCodeForIdToken(code)
    if (!userInfo) {
      return redirectToError(request, oauthState.redirectTo)
    }

    if (
      !canAccessWarRoomDuringLogin({
        email: userInfo.email,
        emailVerified: userInfo.email_verified,
      })
    ) {
      return redirectToUnauthorized(request)
    }

    let resolvedUser: Awaited<ReturnType<typeof resolveUserByEmail>>
    try {
      resolvedUser = await resolveUserByEmail(userInfo.email)
    } catch (error) {
      if (isAhsHttpError(error, 400) || isAhsHttpError(error, 404)) {
        return redirectToUnauthorized(request)
      }
      throw error
    }

    const cookieInfo = await createCookieInfo(
      createSessionCookiePayload({
        userId: resolvedUser.user_id,
        email: resolvedUser.email,
        firstName: getGoogleFirstName(userInfo),
      }),
      request.nextUrl.hostname
    )

    const redirectUrl = new URL(oauthState.redirectTo, request.url)
    const response = NextResponse.redirect(redirectUrl)
    response.cookies.set(cookieInfo.name, cookieInfo.value, cookieInfo.options)
    return response
  } catch (error) {
    unstable_rethrow(error)
    return redirectToError(request)
  }
}
