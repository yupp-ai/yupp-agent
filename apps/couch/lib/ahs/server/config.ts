import 'server-only'

/**
 * Server-side AHS configuration.
 *
 * These helpers intentionally read only server env vars. Nothing here should
 * be imported by client bundles.
 */
export function getAhsHost(): string {
  return process.env.AHS_HOST ?? ''
}

export function getAhsApiKey(): string {
  return process.env.AHS_API_KEY ?? ''
}
