import { describe, expect, it } from 'bun:test'
import { isAllowedOauthInitiatorHost } from './oauth-initiator-host'
import { normalizeOauthRedirectPath } from './oauth-redirect-path'

function withOauthRedirectHost<T>(
  oauthRedirectHost: string | undefined,
  callback: () => T
): T {
  const originalOauthRedirectHost = process.env.OAUTH_REDIRECT_HOST

  if (oauthRedirectHost === undefined) {
    delete process.env.OAUTH_REDIRECT_HOST
  } else {
    process.env.OAUTH_REDIRECT_HOST = oauthRedirectHost
  }

  try {
    return callback()
  } finally {
    if (originalOauthRedirectHost === undefined) {
      delete process.env.OAUTH_REDIRECT_HOST
    } else {
      process.env.OAUTH_REDIRECT_HOST = originalOauthRedirectHost
    }
  }
}

describe('normalizeOauthRedirectPath', () => {
  it('keeps internal paths on the same origin', () => {
    expect(
      normalizeOauthRedirectPath(
        '/session/123?tab=activity#details',
        'https://war-room.example'
      )
    ).toBe('/session/123?tab=activity#details')
  })

  it('normalizes same-origin absolute URLs back to internal paths', () => {
    expect(
      normalizeOauthRedirectPath(
        'https://war-room.example/session/123?tab=activity#details',
        'https://war-room.example'
      )
    ).toBe('/session/123?tab=activity#details')
  })

  it('falls back to the root path for cross-origin redirects', () => {
    expect(
      normalizeOauthRedirectPath(
        'https://evil.example/session/123?tab=activity#details',
        'https://war-room.example'
      )
    ).toBe('/')
  })

  it('falls back to the root path for invalid redirects', () => {
    expect(
      normalizeOauthRedirectPath('https://%', 'https://war-room.example')
    ).toBe('/')
    expect(
      normalizeOauthRedirectPath(undefined, 'https://war-room.example')
    ).toBe('/')
  })
})

describe('isAllowedOauthInitiatorHost', () => {
  it('allows the current origin', () => {
    expect(
      isAllowedOauthInitiatorHost(
        'https://war-room.example',
        'https://war-room.example'
      )
    ).toBe(true)
  })

  it('allows localhost cross-host redirects when the callback host is also local', () => {
    expect(
      isAllowedOauthInitiatorHost(
        'http://localhost:3009',
        'http://127.0.0.1:3000'
      )
    ).toBe(true)
  })

  it('allows preview-to-preview redirects when the configured callback host is a preview deployment', () => {
    withOauthRedirectHost('https://war-room.preview.yuppster.ai', () => {
      expect(
        isAllowedOauthInitiatorHost(
          'https://yupp-agent-git-ax-war-room-auth-shell.preview.yuppster.ai',
          'https://war-room.yupp.ai'
        )
      ).toBe(true)
    })
  })

  it('rejects arbitrary external origins', () => {
    withOauthRedirectHost('https://war-room.yupp.ai', () => {
      expect(
        isAllowedOauthInitiatorHost(
          'https://evil.example',
          'https://war-room.yupp.ai'
        )
      ).toBe(false)
    })
  })

  it('rejects preview redirects when the callback host is not a preview deployment', () => {
    withOauthRedirectHost('https://war-room.yupp.ai', () => {
      expect(
        isAllowedOauthInitiatorHost(
          'https://yupp-agent-git-ax-war-room-auth-shell.preview.yuppster.ai',
          'https://war-room.yupp.ai'
        )
      ).toBe(false)
    })
  })

  it('rejects malformed initiator hosts', () => {
    expect(
      isAllowedOauthInitiatorHost(
        'https://war-room.example/path',
        'https://war-room.example'
      )
    ).toBe(false)
    expect(
      isAllowedOauthInitiatorHost(
        'javascript:alert(1)',
        'https://war-room.example'
      )
    ).toBe(false)
  })
})
