'use client'

import { cn } from '@/lib/utils'
import { Calendar, Folder, Layers, PanelLeft, Plus, Settings } from 'lucide-react'
import Link from 'next/link'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { RecentSessions } from './recent-sessions'

export function LeftRail({
  activeSessionId,
}: {
  activeSessionId?: string
}) {
  const [collapsed, setCollapsed] = useState(false)

  return (
    <aside
      className={cn(
        'flex h-screen flex-col border-r bg-sidebar text-sidebar-foreground transition-all',
        collapsed ? 'w-16' : 'w-64'
      )}
    >
      <div
        className={cn(
          'py-3',
          collapsed
            ? 'flex flex-col items-center gap-3'
            : 'flex items-center justify-between px-3'
        )}
      >
        <Link
          aria-label="home"
          className="flex items-center gap-2"
          href="/"
        >
          <span aria-hidden className="text-lg">
            🛋️
          </span>
          {!collapsed && (
            <span className="font-semibold tracking-tight">Couch</span>
          )}
        </Link>
        <button
          aria-label="toggle rail"
          className="text-muted-foreground hover:text-foreground"
          onClick={() => setCollapsed((v) => !v)}
          type="button"
        >
          <PanelLeft className="h-4 w-4" />
        </button>
      </div>

      {!collapsed && (
        <>
          <nav className="flex flex-col gap-0.5 px-2 py-1">
            <Link
              className="flex items-center gap-2 rounded-md bg-sidebar-accent px-2 py-1.5 text-sm font-medium"
              href="/"
            >
              <Layers className="h-4 w-4" /> Sessions
            </Link>
            <Tooltip>
              <TooltipTrigger
                className="flex cursor-not-allowed items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground/60"
              >
                <span className="flex items-center gap-2">
                  <Folder className="h-4 w-4" /> Projects
                </span>
              </TooltipTrigger>
              <TooltipContent>Coming soon</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger
                className="flex cursor-not-allowed items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground/60"
              >
                <span className="flex items-center gap-2">
                  <Calendar className="h-4 w-4" /> Schedules
                </span>
              </TooltipTrigger>
              <TooltipContent>Coming soon</TooltipContent>
            </Tooltip>
          </nav>

          <div className="mt-3 flex items-center justify-between px-3">
            <span className="text-muted-foreground text-xs uppercase tracking-wider">
              Recent
            </span>
            <Link aria-label="new session" href="/">
              <Plus className="h-4 w-4 text-muted-foreground hover:text-foreground" />
            </Link>
          </div>

          <div className="flex-1 overflow-y-auto px-2 py-1">
            <RecentSessions activeId={activeSessionId} />
          </div>

          <SettingsButton />
        </>
      )}
    </aside>
  )
}

function SettingsButton() {
  const [open, setOpen] = useState(false)
  return (
    <div className="border-t px-2 py-2">
      <Button
        className="w-full justify-start"
        onClick={() => setOpen(true)}
        size="sm"
        variant="ghost"
      >
        <Settings className="mr-2 h-4 w-4" /> Settings
      </Button>
      {open && <SettingsDialog onClose={() => setOpen(false)} />}
    </div>
  )
}

function SettingsDialog({ onClose }: { onClose: () => void }) {
  return (
    <button
      aria-label="close settings"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
      type="button"
    >
      <div
        className="w-full max-w-md rounded-2xl border bg-card p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
        role="dialog"
      >
        <h2 className="font-semibold text-lg">Settings</h2>
        <p className="mt-2 text-muted-foreground text-sm">
          More settings coming soon.
        </p>
        <form
          action="/api/authentication/logout"
          className="mt-4"
          method="post"
        >
          <Button className="w-full" type="submit" variant="outline">
            Sign out
          </Button>
        </form>
        <Button className="mt-2 w-full" onClick={onClose} variant="ghost">
          Close
        </Button>
      </div>
    </button>
  )
}
