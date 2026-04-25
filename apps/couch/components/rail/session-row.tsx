'use client'

import { cn } from '@/lib/utils'
import {
  BarChart3,
  Calendar,
  Copy,
  MoreHorizontal,
  Pencil,
  Pin,
  Plus,
} from 'lucide-react'
import Link from 'next/link'
import { useState } from 'react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { relativeTime } from '@/lib/format'

export interface SessionRowData {
  session_id: string
  title?: string | null
  agent_name: string
  status: string
  created_at: string
}

export function SessionRow({
  data,
  active,
  pinned,
  customTitle,
  onTogglePin,
  onRename,
}: {
  data: SessionRowData
  active?: boolean
  pinned?: boolean
  customTitle?: string
  onTogglePin: () => void
  onRename: (next: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(customTitle ?? data.title ?? '')
  const display = customTitle ?? data.title ?? data.session_id.slice(0, 8)

  return (
    <div
      className={cn(
        'group flex items-start gap-1 rounded-md px-2 py-1.5 hover:bg-sidebar-accent',
        active && 'bg-sidebar-accent'
      )}
    >
      <Link className="min-w-0 flex-1" href={`/s/${data.session_id}`}>
        {editing ? (
          <input
            autoFocus
            className="w-full bg-transparent text-sm focus:outline-none"
            onBlur={() => {
              onRename(draft)
              setEditing(false)
            }}
            onChange={(e) => setDraft(e.target.value)}
            onClick={(e) => e.preventDefault()}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                onRename(draft)
                setEditing(false)
              } else if (e.key === 'Escape') {
                setDraft(customTitle ?? data.title ?? '')
                setEditing(false)
              }
            }}
            value={draft}
          />
        ) : (
          <>
            <div className="truncate text-sm font-medium">
              {pinned && <Pin className="mr-1 inline h-3 w-3" />}
              {display}
            </div>
            <div className="text-muted-foreground text-xs">
              {relativeTime(new Date(data.created_at))}
            </div>
          </>
        )}
      </Link>
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="session actions"
          className="opacity-0 focus:opacity-100 group-hover:opacity-100"
        >
          <MoreHorizontal className="h-4 w-4 text-muted-foreground" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={onTogglePin}>
            <Pin className="mr-2 h-4 w-4" />
            {pinned ? 'Unpin' : 'Pin'}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setEditing(true)}>
            <Pencil className="mr-2 h-4 w-4" /> Rename
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => navigator.clipboard.writeText(data.session_id)}
          >
            <Copy className="mr-2 h-4 w-4" /> Copy session ID
          </DropdownMenuItem>
          <DropdownMenuItem disabled>
            <Plus className="mr-2 h-4 w-4" /> Start duplicate session
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem disabled>
            <Calendar className="mr-2 h-4 w-4" /> Schedule
          </DropdownMenuItem>
          <DropdownMenuItem disabled>
            <BarChart3 className="mr-2 h-4 w-4" /> Analyze session
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
