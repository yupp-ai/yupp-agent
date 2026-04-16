'use client'

import { usePathname, useSearchParams } from 'next/navigation'
import { useState } from 'react'
import { buttonVariants } from '@/components/ui/button'
import { cn } from '@/components/ui/utils'

const ERROR_MESSAGES: Record<string, string> = {
  authentication: 'Oops. Try again!',
  unauthorized: 'Only Yuppsters can access War Room.',
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
        headers: {
          'Content-Type': 'application/json',
        },
      })

      if (!response.ok) {
        throw new Error(await response.text())
      }

      const data = (await response.json()) as {
        redirectUrl?: string
      }

      if (!data.redirectUrl) {
        throw new Error('Missing redirect URL')
      }

      window.location.href = data.redirectUrl
    } catch {
      setSubmitError('Unable to start Google sign-in.')
      setIsLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-950 px-4">
      <div className="w-full max-w-md rounded-3xl border border-zinc-800 bg-zinc-900/70 p-8 shadow-2xl shadow-black/30">
        <div className="space-y-3">
          <h1 className="font-bold text-3xl tracking-tight text-zinc-50">
            War Room
          </h1>
          <p className="text-sm text-zinc-400">
            Sign in with your Google account to create, inspect, and control AHS
            sessions.
          </p>
        </div>

        <div className="mt-8 space-y-4">
          <button
            className={cn(
              buttonVariants({ variant: 'primary' }),
              'w-full justify-center py-2.5'
            )}
            onClick={handleLogin}
            type="button"
          >
            {isLoading ? 'Preparing...' : 'Continue with Google'}
          </button>

          {(submitError || error) && (
            <p className="text-sm text-red-400">
              {submitError ??
                ERROR_MESSAGES[error ?? ''] ??
                'Unable to sign in.'}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
