'use client'

import { useEffect, useState } from 'react'
import { useSession } from './session-provider'

export function SessionExpirationNotice() {
  const session = useSession()
  const [isExpired, setIsExpired] = useState(false)

  useEffect(() => {
    const expiresAtMs = Date.parse(session.expiresAt)
    if (Number.isNaN(expiresAtMs)) {
      setIsExpired(true)
      return
    }

    const syncExpirationState = () => {
      const expired = Date.now() >= expiresAtMs
      setIsExpired(expired)
      return expired
    }

    if (syncExpirationState()) {
      return
    }

    const timeoutId = window.setTimeout(() => {
      setIsExpired(true)
    }, expiresAtMs - Date.now())

    window.addEventListener('focus', syncExpirationState)

    return () => {
      window.clearTimeout(timeoutId)
      window.removeEventListener('focus', syncExpirationState)
    }
  }, [session.expiresAt])

  if (!isExpired) {
    return null
  }

  return (
    <div
      aria-live="polite"
      className="border-b border-amber-400/30 bg-amber-500/10 px-4 py-2 text-amber-100"
      role="status"
    >
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-3">
        <p className="text-sm">
          Login required. Your Couch session has expired.
        </p>
        <form action="/api/authentication/logout" method="post">
          <button
            className="shrink-0 rounded-md border border-amber-300/30 bg-amber-400/10 px-3 py-1 text-xs font-medium text-amber-50 transition-colors hover:bg-amber-400/20"
            type="submit"
          >
            Sign in again
          </button>
        </form>
      </div>
    </div>
  )
}
