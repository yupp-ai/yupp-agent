import 'server-only'

import { createHmac, timingSafeEqual } from 'node:crypto'
import type { cookies } from 'next/headers'
import type { NextResponse } from 'next/server'
import { z } from 'zod'
import { isLocalDevelopment, isProduction } from './environments'

const useSecureCookie = !isLocalDevelopment
const nonProductionSuffix = isProduction ? '' : '-non-prod'
const SESSION_TTL_MS = 1000 * 60 * 60 * 24 * 7

export const SESSION_COOKIE_NAME = useSecureCookie
  ? `__Secure-yupp.session-token${nonProductionSuffix}`
  : `yupp.session-token${nonProductionSuffix}`

const SessionCookiePayloadSchema = z.object({
  userId: z.string(),
  email: z.string().email(),
  firstName: z.string().min(1).optional(),
  expiresAt: z.string().datetime(),
  version: z.literal(2),
})

export type SessionCookiePayload = z.infer<typeof SessionCookiePayloadSchema>

function getSessionExpirationDate(now = new Date()) {
  return new Date(now.getTime() + SESSION_TTL_MS)
}

export function createSessionCookiePayload(
  input: {
    userId: string
    email: string
    firstName?: string
  },
  expiresAt = getSessionExpirationDate()
): SessionCookiePayload {
  const firstName = input.firstName?.trim() || undefined

  return {
    userId: input.userId,
    email: input.email.toLowerCase(),
    ...(firstName ? { firstName } : {}),
    expiresAt: expiresAt.toISOString(),
    version: 2,
  }
}

export const deleteSessionCookie = (
  cookieStoreLike:
    | Awaited<ReturnType<typeof cookies>>
    | ReturnType<typeof NextResponse.redirect>['cookies'],
  hostname?: string
) => {
  const cookieDomain = getParentDomain(hostname)

  cookieStoreLike.delete({
    name: SESSION_COOKIE_NAME,
    httpOnly: true,
    secure: useSecureCookie,
    sameSite: 'lax',
    ...(cookieDomain && !isProduction ? { domain: cookieDomain } : {}),
  })
}

export async function unsafeGetSessionFromCookie(cookieStoreLike: {
  get: (key: string) => { value: string } | undefined
}) {
  const sessionCookie = cookieStoreLike.get(SESSION_COOKIE_NAME)
  if (!sessionCookie?.value) {
    return null
  }

  try {
    const payload = verifyCookiePayload(sessionCookie.value)
    return SessionCookiePayloadSchema.parse(payload)
  } catch {
    return null
  }
}

export async function createCookieInfo(
  payload: SessionCookiePayload,
  hostname?: string
) {
  const cookieDomain = getParentDomain(hostname)
  return {
    name: SESSION_COOKIE_NAME,
    value: signCookiePayload(payload),
    options: {
      expires: new Date(payload.expiresAt),
      httpOnly: true,
      secure: useSecureCookie,
      path: '/',
      sameSite: 'lax' as const,
      ...(cookieDomain && !isProduction ? { domain: cookieDomain } : {}),
    },
  }
}

const YUPP_PREVIEW_DOMAIN = '.preview.yuppster.ai'

function getParentDomain(hostname?: string): string | undefined {
  if (isProduction) {
    return undefined
  }
  if (isLocalDevelopment || !hostname) {
    return undefined
  }
  if (hostname.includes(YUPP_PREVIEW_DOMAIN)) {
    return YUPP_PREVIEW_DOMAIN
  }
  return undefined
}

function encodeBase64Url(value: Buffer | string): string {
  return Buffer.from(value).toString('base64url')
}

function decodeBase64Url(value: string): Buffer {
  return Buffer.from(value, 'base64url')
}

function cookieSecret(): string {
  const secret = process.env.AUTH_SECRET
  if (!secret) {
    throw new Error('AUTH_SECRET is not set')
  }
  return secret
}

function signJwtSegment(value: string): Buffer {
  return createHmac('sha256', cookieSecret()).update(value).digest()
}

function signCookiePayload(payload: SessionCookiePayload): string {
  const headerJson = JSON.stringify({ alg: 'HS256', typ: 'JWT' })
  const payloadJson = JSON.stringify(payload)
  const encodedHeader = encodeBase64Url(headerJson)
  const encodedPayload = encodeBase64Url(payloadJson)
  const signatureMessage = `${encodedHeader}.${encodedPayload}`
  const signature = encodeBase64Url(signJwtSegment(signatureMessage))
  return `${signatureMessage}.${signature}`
}

function verifyCookiePayload(cookieValue: string): unknown {
  const [encodedHeader, encodedPayload, encodedSignature] =
    cookieValue.split('.')
  if (!encodedHeader || !encodedPayload || !encodedSignature) {
    throw new Error('Invalid cookie')
  }

  const signatureMessage = `${encodedHeader}.${encodedPayload}`
  const expectedSignature = signJwtSegment(signatureMessage)
  const actualSignature = decodeBase64Url(encodedSignature)

  if (
    expectedSignature.length !== actualSignature.length ||
    !timingSafeEqual(expectedSignature, actualSignature)
  ) {
    throw new Error('Invalid cookie signature')
  }

  const header = JSON.parse(
    decodeBase64Url(encodedHeader).toString('utf8')
  ) as {
    alg?: string
  }
  if (header.alg !== 'HS256') {
    throw new Error('Invalid cookie algorithm')
  }

  return JSON.parse(decodeBase64Url(encodedPayload).toString('utf8'))
}
