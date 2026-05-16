import 'server-only'

import { cookies } from 'next/headers'
import { redirect } from 'next/navigation'
import type { NextRequest } from 'next/server'
import { z } from 'zod'
import { isLocalDevelopment } from './environments'
import { isAllowedOauthInitiatorHost } from './oauth-initiator-host'
import { normalizeOauthRedirectPath } from './oauth-redirect-path'

export const GOOGLE_CSRF_COOKIE_NAME = 'yupp.auth.csrf.google'

const oAuthStateSchema = z.object({
  csrf: z.string(),
  redirectTo: z.string(),
  oauthInitiatorHost: z.string(),
  additionalData: z.string().optional(),
})

function encodeBase64Url(value: string): string {
  return Buffer.from(value).toString('base64url')
}

function decodeBase64Url(value: string): string {
  return Buffer.from(value, 'base64url').toString('utf8')
}

function getSecureCookieName(cookieName: string) {
  return isLocalDevelopment ? cookieName : `__Secure-${cookieName}`
}

export async function initOauthState(
  request: NextRequest,
  csrfCookieName: string,
  callbackPath: string,
  additionalData?: string
): Promise<string> {
  const csrfToken = crypto.randomUUID()
  const cookieStore = await cookies()
  const redirectTo = normalizeOauthRedirectPath(
    request.nextUrl.searchParams.get('redirectTo'),
    request.nextUrl.origin
  )

  cookieStore.set(getSecureCookieName(csrfCookieName), csrfToken, {
    httpOnly: true,
    secure: !isLocalDevelopment,
    path: callbackPath,
    maxAge: 60 * 15,
    sameSite: 'lax',
  })

  return encodeBase64Url(
    JSON.stringify({
      csrf: csrfToken,
      redirectTo,
      oauthInitiatorHost: request.nextUrl.origin,
      additionalData,
    })
  )
}

export function decodeAndValidateOauthState(
  encodedState: string,
  request: NextRequest,
  csrfCookieName: string,
  callbackPath: string
):
  | { isValid: true; redirectTo: string; additionalData?: string }
  | { isValid: false; redirectTo?: string; additionalData?: string } {
  let decodedState: z.infer<typeof oAuthStateSchema> | undefined

  try {
    decodedState = oAuthStateSchema.parse(JSON.parse(decodeBase64Url(encodedState)))
  } catch {
    return { isValid: false }
  }

  const currentHost = request.nextUrl.origin
  if (!isAllowedOauthInitiatorHost(decodedState.oauthInitiatorHost, currentHost)) {
    return {
      isValid: false,
      redirectTo: normalizeOauthRedirectPath(decodedState.redirectTo, currentHost),
      additionalData: decodedState.additionalData,
    }
  }

  const oauthInitiatorHost = decodedState.oauthInitiatorHost
  if (oauthInitiatorHost !== currentHost) {
    const redirectUrl = new URL(callbackPath, oauthInitiatorHost)
    redirectUrl.search = request.nextUrl.searchParams.toString()
    redirect(redirectUrl.toString())
  }

  const redirectTo = normalizeOauthRedirectPath(decodedState.redirectTo, oauthInitiatorHost)

  const storedCsrfToken = request.cookies.get(getSecureCookieName(csrfCookieName))?.value

  if (!storedCsrfToken || storedCsrfToken !== decodedState.csrf) {
    return {
      isValid: false,
      redirectTo,
      additionalData: decodedState.additionalData,
    }
  }

  return {
    isValid: true,
    redirectTo,
    additionalData: decodedState.additionalData,
  }
}
