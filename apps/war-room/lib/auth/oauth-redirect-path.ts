const DEFAULT_REDIRECT_PATH = '/'

export function normalizeOauthRedirectPath(
  redirectTo: string | null | undefined,
  requestOrigin: string
): string {
  if (!redirectTo) {
    return DEFAULT_REDIRECT_PATH
  }

  try {
    const normalizedUrl = new URL(redirectTo, requestOrigin)

    if (normalizedUrl.origin !== requestOrigin) {
      return DEFAULT_REDIRECT_PATH
    }

    return `${normalizedUrl.pathname}${normalizedUrl.search}${normalizedUrl.hash}`
  } catch {
    return DEFAULT_REDIRECT_PATH
  }
}
