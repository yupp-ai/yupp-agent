'use client'

import { usePathname, useSearchParams } from 'next/navigation'
import { useState } from 'react'
import { Button } from '@/components/ui/button'

const ERROR_MESSAGES: Record<string, string> = {
  authentication: 'Oops. Try again!',
  unauthorized: 'Only Yuppsters can access Couch.',
  session_invalidated: 'Your session is no longer valid. Sign in again.',
}

export function LoginScreen() {
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const error = searchParams.get('error')
  const [isLoading, setIsLoading] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const nextSearchParams = new URLSearchParams(searchParams.toString())
  nextSearchParams.delete('error')
  const next = `${pathname}${nextSearchParams.toString() ? `?${nextSearchParams.toString()}` : ''}`
  const loginUrl = `/api/authentication/google/login?redirectTo=${encodeURIComponent(next)}`

  async function handleLogin() {
    setIsLoading(true)
    setSubmitError(null)
    try {
      const response = await fetch(loginUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      })
      if (!response.ok) throw new Error(await response.text())
      const data = (await response.json()) as { redirectUrl?: string }
      if (!data.redirectUrl) throw new Error('Missing redirect URL')
      window.location.href = data.redirectUrl
    } catch {
      setSubmitError('Unable to start Google sign-in.')
      setIsLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-md rounded-2xl border bg-card p-8 shadow-sm">
        <div className="space-y-2">
          <p className="text-3xl">🛋️</p>
          <h1 className="text-2xl font-semibold tracking-tight">Couch</h1>
          <p className="text-sm text-muted-foreground">
            Sign in with your Google account to start agent sessions.
          </p>
        </div>
        <div className="mt-8 space-y-4">
          <Button
            className="w-full justify-center"
            onClick={handleLogin}
            type="button"
          >
            {isLoading ? 'Preparing…' : 'Continue with Google'}
          </Button>
          {(submitError || error) && (
            <p className="text-sm text-destructive">
              {submitError ?? ERROR_MESSAGES[error ?? ''] ?? 'Unable to sign in.'}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
