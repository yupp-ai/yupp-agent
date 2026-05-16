const LOCALHOST_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]'])

function parseOrigin(origin: string): URL | null {
  try {
    const url = new URL(origin)
    if (
      url.origin !== origin ||
      (url.protocol !== 'http:' && url.protocol !== 'https:')
    ) {
      return null
    }
    return url
  } catch {
    return null
  }
}

function getConfiguredOauthRedirectHost(): URL | null {
  const configuredHost = process.env.OAUTH_REDIRECT_HOST
  if (!configuredHost) {
    return null
  }
  return parseOrigin(configuredHost)
}

function isLocalhostOrigin(url: URL): boolean {
  return LOCALHOST_HOSTNAMES.has(url.hostname)
}

export function isAllowedOauthInitiatorHost(
  oauthInitiatorHost: string,
  currentHost: string
): boolean {
  const candidateUrl = parseOrigin(oauthInitiatorHost)
  const currentUrl = parseOrigin(currentHost)

  if (!candidateUrl || !currentUrl) {
    return false
  }

  if (candidateUrl.origin === currentUrl.origin) {
    return true
  }

  const configuredUrl = getConfiguredOauthRedirectHost()
  if (configuredUrl && candidateUrl.origin === configuredUrl.origin) {
    return true
  }

  const trustedOrigins = configuredUrl ? [currentUrl, configuredUrl] : [currentUrl]

  if (isLocalhostOrigin(candidateUrl)) {
    return trustedOrigins.some(isLocalhostOrigin)
  }

  return false
}
