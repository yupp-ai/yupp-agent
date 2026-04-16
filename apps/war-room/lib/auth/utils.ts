import 'server-only'

export function decodeIdToken(idToken: string): object {
  const [, payload] = idToken.split('.')
  if (!payload) {
    throw new Error('Invalid ID token')
  }

  try {
    return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'))
  } catch (error) {
    throw new Error('Invalid ID token', {
      cause: error,
    })
  }
}
