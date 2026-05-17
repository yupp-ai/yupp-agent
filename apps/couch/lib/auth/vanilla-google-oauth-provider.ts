import 'server-only'

import { z } from 'zod'
import { decodeIdToken } from './utils'

const TokenResponseSchema = z.object({
  id_token: z.string(),
})

const GoogleUserInfoSchema = z.object({
  aud: z.string(),
  email: z.string().email(),
  email_verified: z.boolean().optional(),
  given_name: z.string().optional(),
  hd: z.string().optional(),
  iss: z.string(),
  name: z.string().optional(),
  picture: z.string().optional(),
  sub: z.string(),
})

export type GoogleUserInfo = z.infer<typeof GoogleUserInfoSchema>

// COUCH_OAUTH_REDIRECT_URL is the production name (matches the shared
// /data/ahs/.env convention); OAUTH_REDIRECT_HOST is the local fallback.
// Either way the value is a base URL — we append the callback path.
const appHost =
  process.env.COUCH_OAUTH_REDIRECT_URL ||
  process.env.OAUTH_REDIRECT_HOST ||
  'http://localhost:3010'
const REDIRECT_URI = `${appHost.replace(/\/$/, '')}/api/authentication/google/callback`
const GOOGLE_ISSUERS = new Set(['accounts.google.com', 'https://accounts.google.com'])

function getGoogleClientId(): string {
  const value = process.env.COUCH_GOOGLE_CLIENT_ID ?? process.env.GOOGLE_CLIENT_ID
  if (!value) {
    throw new Error('COUCH_GOOGLE_CLIENT_ID (or GOOGLE_CLIENT_ID) is not set')
  }
  return value
}

function getGoogleClientSecret(): string {
  const value = process.env.COUCH_GOOGLE_CLIENT_SECRET ?? process.env.GOOGLE_CLIENT_SECRET
  if (!value) {
    throw new Error('COUCH_GOOGLE_CLIENT_SECRET (or GOOGLE_CLIENT_SECRET) is not set')
  }
  return value
}

export function getGoogleAuthUrl({ state }: { state: string }): string {
  const params = new URLSearchParams({
    client_id: getGoogleClientId(),
    redirect_uri: REDIRECT_URI,
    response_type: 'code',
    scope: 'openid email profile',
    state,
    prompt: 'select_account',
  })

  return `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`
}

export async function exchangeCodeForIdToken(code: string): Promise<GoogleUserInfo | null> {
  const params = new URLSearchParams({
    client_id: getGoogleClientId(),
    client_secret: getGoogleClientSecret(),
    code,
    grant_type: 'authorization_code',
    redirect_uri: REDIRECT_URI,
  })

  try {
    const response = await fetch('https://oauth2.googleapis.com/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: params.toString(),
    })

    const data: unknown = await response.json()
    if (!response.ok) {
      return null
    }

    const tokenData = TokenResponseSchema.parse(data)
    const userInfo = GoogleUserInfoSchema.parse(decodeIdToken(tokenData.id_token))

    if (userInfo.aud !== getGoogleClientId() || !GOOGLE_ISSUERS.has(userInfo.iss)) {
      return null
    }

    return userInfo
  } catch {
    return null
  }
}
