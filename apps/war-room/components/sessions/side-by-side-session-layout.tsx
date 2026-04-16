'use client'

import { useEffect, useState } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { SessionViewer } from './session-viewer'

const DESKTOP_BREAKPOINT_QUERY = '(min-width: 1024px)'

interface SideBySideSessionLayoutProps {
  leftSessionId: string
  rightSessionId: string
}

function formatSessionTabLabel(sessionId: string): string {
  if (sessionId.length <= 14) {
    return sessionId
  }

  return `${sessionId.slice(0, 6)}...${sessionId.slice(-4)}`
}

export function SideBySideSessionLayout({
  leftSessionId,
  rightSessionId,
}: SideBySideSessionLayoutProps) {
  const [isDesktop, setIsDesktop] = useState<boolean | null>(null)
  const [activeSessionId, setActiveSessionId] = useState(leftSessionId)

  useEffect(() => {
    setActiveSessionId(leftSessionId)
  }, [leftSessionId])

  useEffect(() => {
    const mediaQueryList = window.matchMedia(DESKTOP_BREAKPOINT_QUERY)

    const updateIsDesktop = (event?: MediaQueryListEvent) => {
      setIsDesktop(event ? event.matches : mediaQueryList.matches)
    }

    updateIsDesktop()
    mediaQueryList.addEventListener('change', updateIsDesktop)

    return () => {
      mediaQueryList.removeEventListener('change', updateIsDesktop)
    }
  }, [])

  if (isDesktop === null) {
    return (
      <div className="p-4">
        <Skeleton className="h-[calc(100svh-3rem)] w-full rounded-2xl" />
      </div>
    )
  }

  if (isDesktop) {
    return (
      <div className="h-[calc(100svh-3rem)] p-4">
        <div className="grid h-full gap-4 lg:grid-cols-2">
          <div className="min-h-0">
            <SessionViewer layout="pane" sessionId={leftSessionId} />
          </div>
          <div className="min-h-0">
            <SessionViewer layout="pane" sessionId={rightSessionId} />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="p-4">
      <Tabs
        className="h-[calc(100svh-3rem)] gap-4"
        onValueChange={setActiveSessionId}
        value={activeSessionId}
      >
        <TabsList className="w-full">
          <TabsTrigger
            className="min-w-0 font-mono text-xs"
            value={leftSessionId}
          >
            {formatSessionTabLabel(leftSessionId)}
          </TabsTrigger>
          <TabsTrigger
            className="min-w-0 font-mono text-xs"
            value={rightSessionId}
          >
            {formatSessionTabLabel(rightSessionId)}
          </TabsTrigger>
        </TabsList>

        <TabsContent className="min-h-0" value={leftSessionId}>
          {activeSessionId === leftSessionId ? (
            <SessionViewer layout="pane" sessionId={leftSessionId} />
          ) : null}
        </TabsContent>

        <TabsContent className="min-h-0" value={rightSessionId}>
          {activeSessionId === rightSessionId ? (
            <SessionViewer layout="pane" sessionId={rightSessionId} />
          ) : null}
        </TabsContent>
      </Tabs>
    </div>
  )
}
