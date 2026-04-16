'use client'

import { QueryClientProvider } from '@tanstack/react-query'
import {
  createTRPCClient,
  httpBatchLink,
  isTRPCClientError,
  type TRPCLink,
} from '@trpc/client'
import { observable } from '@trpc/server/observable'
import { createTRPCContext } from '@trpc/tanstack-react-query'
import type { ReactNode } from 'react'
import { useState } from 'react'
import type { AppActions } from '@/server/trpc/actions'
import { getQueryClient } from '@/server/trpc/query-client'

const trpc = createTRPCContext<AppActions>()

export const { useTRPC, useTRPCClient } = trpc

const AUTHENTICATION_ERROR = 'session_invalidated'

let trpcClientSingleton: ReturnType<
  typeof createTRPCClient<AppActions>
> | null = null
let isHandlingAuthenticationFailure = false

function redirectToLoginForAuthenticationFailure() {
  if (typeof window === 'undefined' || isHandlingAuthenticationFailure) {
    return
  }

  isHandlingAuthenticationFailure = true

  const url = new URL(window.location.href)
  url.searchParams.set('error', AUTHENTICATION_ERROR)
  window.location.replace(url.toString())
}

const authFailureLink: TRPCLink<AppActions> = () => {
  return ({ next, op }) =>
    observable((observer) => {
      const subscription = next(op).subscribe({
        next(result) {
          observer.next(result)
        },
        error(error) {
          if (
            isTRPCClientError<AppActions>(error) &&
            error.data?.code === 'UNAUTHORIZED'
          ) {
            redirectToLoginForAuthenticationFailure()
          }

          observer.error(error)
        },
        complete() {
          observer.complete()
        },
      })

      return () => {
        subscription.unsubscribe()
      }
    })
}

function getTRPCClient() {
  if (!trpcClientSingleton) {
    trpcClientSingleton = createTRPCClient<AppActions>({
      links: [
        authFailureLink,
        httpBatchLink({
          url: '/api/trpc',
        }),
      ],
    })
  }
  return trpcClientSingleton
}

export function TRPCProvider({ children }: { children: ReactNode }) {
  const queryClient = getQueryClient()
  const [trpcClient] = useState(getTRPCClient)

  return (
    <trpc.TRPCProvider trpcClient={trpcClient} queryClient={queryClient}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </trpc.TRPCProvider>
  )
}
