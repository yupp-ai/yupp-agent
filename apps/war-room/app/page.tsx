'use client'

import { useMutation, useQuery } from '@tanstack/react-query'
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  type PromptInputMessage,
  PromptInputProvider,
  PromptInputSubmit,
  PromptInputTextarea,
  usePromptInputController,
} from '@yupp/agents-ui/prompt-input'
import Link from 'next/link'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { statusColor } from '@/lib/ahs/session-status'
import { getSessionDisplayTitle } from '@/lib/ahs/session-title'
import type { AhsSessionInfo } from '@/lib/ahs/types'
import { useSession } from '@/lib/auth/session-provider'
import { useTRPC } from '@/lib/hooks/trpc-client'
import { useAutofocusOnKeyPress } from '@/lib/hooks/use-autofocus-on-key-press'
import {
  persistSelectedAgent,
  usePersistedAgentSelection,
} from '@/lib/hooks/use-persisted-agent-selection'

function getSessionCreatedAtTime(value: string | null): number {
  if (!value) return 0

  const parsedValue = Date.parse(value)
  return Number.isNaN(parsedValue) ? 0 : parsedValue
}

function formatSessionCreatedAt(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function RecentSessionLink({ session }: { session: AhsSessionInfo }) {
  return (
    <Link
      className="flex items-center justify-between gap-4 rounded-lg px-1 py-1.5 transition-colors hover:text-zinc-100"
      href={`/session/${session.session_id}`}
    >
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${statusColor(session.status)}`}
        />
        <p className="min-w-0 flex-1 truncate font-medium text-sm text-zinc-100">
          {getSessionDisplayTitle(session)}
        </p>
      </div>
      <span className="shrink-0 text-right text-xs text-zinc-500">
        {formatSessionCreatedAt(session.created_at)}
      </span>
    </Link>
  )
}

export default function HomePage() {
  return (
    <PromptInputProvider>
      <HomePageContent />
    </PromptInputProvider>
  )
}

function HomePageContent() {
  const pathname = usePathname()
  const router = useRouter()
  const searchParams = useSearchParams()
  const trpc = useTRPC()
  const session = useSession()
  const promptInput = usePromptInputController()
  const promptTextareaRef = useRef<HTMLTextAreaElement | null>(null)
  const handledRequestedAgentRef = useRef<string | null>(null)
  const [isAgentSelectOpen, setIsAgentSelectOpen] = useState(false)

  const handlePromptMount = useCallback((node: HTMLDivElement | null) => {
    promptTextareaRef.current = node?.querySelector('textarea') ?? null
  }, [])

  const {
    data: agents = [],
    isLoading,
    isSuccess,
    error: loadError,
  } = useQuery(trpc.ahs.listAgents.queryOptions())
  const {
    data: recentSessions = [],
    isLoading: isRecentSessionsLoading,
    error: recentSessionsError,
  } = useQuery({
    ...trpc.ahs.listSessions.queryOptions({
      include_all: false,
      limit: 3,
    }),
    select: (data) =>
      [...data.sessions].sort(
        (left, right) =>
          getSessionCreatedAtTime(right.created_at) -
          getSessionCreatedAtTime(left.created_at)
      ),
  })

  const { resolvedAgent, setSelectedAgent } = usePersistedAgentSelection(
    agents,
    isSuccess
  )
  const requestedAgent = searchParams.get('agent')

  const createSession = useMutation(
    trpc.ahs.createSession.mutationOptions({
      onSuccess(data) {
        router.push(`/session/${data.session_id}`)
      },
    })
  )

  const error = loadError?.message ?? createSession.error?.message ?? null
  const hasMessage = promptInput.textInput.value.trim().length > 0
  const heading = session.user.firstName
    ? `Welcome back, ${session.user.firstName}!`
    : 'Welcome back!'

  useEffect(() => {
    if (!requestedAgent || !isSuccess) {
      return
    }

    if (handledRequestedAgentRef.current === requestedAgent) {
      return
    }

    handledRequestedAgentRef.current = requestedAgent

    if (agents.some((agent) => agent.name === requestedAgent)) {
      persistSelectedAgent(requestedAgent)
      setSelectedAgent(requestedAgent)
    }

    const nextSearchParams = new URLSearchParams(searchParams.toString())
    nextSearchParams.delete('agent')
    const nextQueryString = nextSearchParams.toString()
    const nextUrl = nextQueryString
      ? `${pathname}?${nextQueryString}`
      : pathname

    router.replace(nextUrl)
  }, [
    agents,
    isSuccess,
    pathname,
    requestedAgent,
    router,
    searchParams,
    setSelectedAgent,
  ])

  useEffect(() => {
    if (pathname !== '/') {
      return
    }

    if (typeof window === 'undefined') {
      return
    }

    const isTouchDevice =
      'ontouchstart' in window || navigator.maxTouchPoints > 0
    const isDesktop = window.matchMedia('(min-width: 1024px)').matches

    if (isTouchDevice || !isDesktop || isAgentSelectOpen) {
      return
    }

    const animationFrameId = window.requestAnimationFrame(() => {
      const textarea = promptTextareaRef.current

      if (!textarea || textarea.disabled || textarea.readOnly) {
        return
      }

      textarea.focus()
    })

    return () => {
      window.cancelAnimationFrame(animationFrameId)
    }
  }, [isAgentSelectOpen, pathname])

  useAutofocusOnKeyPress(promptTextareaRef, !isAgentSelectOpen)

  function handleSubmit({ text }: PromptInputMessage) {
    const message = text.trim()
    if (!resolvedAgent || !message) return
    createSession.mutate({
      agent_id: resolvedAgent,
      trigger: 'API',
      message,
    })
  }

  return (
    <main className="flex min-h-[calc(100svh-3rem)] flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-xl space-y-6">
        <div>
          <h1 className="-mt-10 mb-6 font-bold text-3xl tracking-tight text-zinc-50 text-center">
            {heading}
          </h1>

          <div ref={handlePromptMount}>
            <PromptInput onSubmit={handleSubmit}>
              <PromptInputBody>
                <PromptInputTextarea
                  autoComplete="off"
                  placeholder="Ask anything"
                />
              </PromptInputBody>
              {/* Upstream fix needed: InputGroupAddon should not force justify-end for the block-end variant. */}
              <PromptInputFooter className="justify-between!">
                <div className="min-w-0 max-w-48">
                  {isLoading ? (
                    <Skeleton className="h-6 w-24 rounded-md" />
                  ) : (
                    <Select
                      className="border-0 bg-transparent px-0 py-1 text-xs"
                      listClassName="min-w-48"
                      onOpenChange={setIsAgentSelectOpen}
                      onValueChange={setSelectedAgent}
                      options={agents.map((agent) => ({
                        label: agent.display_name,
                        value: agent.name,
                      }))}
                      placeholder="Select agent"
                      value={resolvedAgent}
                    />
                  )}
                </div>
                <PromptInputSubmit
                  disabled={
                    createSession.isPending || !resolvedAgent || !hasMessage
                  }
                  status={createSession.isPending ? 'submitted' : undefined}
                />
              </PromptInputFooter>
            </PromptInput>
          </div>

          {error && <p className="mt-4 text-sm text-red-400">{error}</p>}
        </div>

        <Card size="sm">
          <CardContent className="space-y-2">
            {isRecentSessionsLoading &&
              [1, 2, 3].map((key) => (
                <div
                  className="flex items-center justify-between gap-4 rounded-lg px-1 py-1.5"
                  key={key}
                >
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <Skeleton className="h-2 w-2 rounded-full" />
                    <Skeleton className="h-5 w-full max-w-64" />
                  </div>
                  <Skeleton className="h-4 w-28" />
                </div>
              ))}

            {recentSessionsError && (
              <p className="text-sm text-red-400">
                {recentSessionsError.message}
              </p>
            )}

            {!isRecentSessionsLoading &&
              !recentSessionsError &&
              recentSessions.length === 0 && (
                <p className="text-sm text-zinc-400">
                  You haven&apos;t started a session yet.
                </p>
              )}

            {!isRecentSessionsLoading &&
              !recentSessionsError &&
              recentSessions.map((recentSession) => (
                <RecentSessionLink
                  key={recentSession.session_id}
                  session={recentSession}
                />
              ))}
          </CardContent>
        </Card>
      </div>
    </main>
  )
}
