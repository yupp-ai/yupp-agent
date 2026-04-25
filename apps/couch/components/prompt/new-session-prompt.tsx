'use client'

import { useQuery } from '@tanstack/react-query'
import { ArrowUp } from 'lucide-react'
import { useEffect, useState, useTransition } from 'react'
import { createSessionAction } from '@/app/_actions/create-session'
import { listAgentsAction } from '@/app/_actions/list-agents'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

const LAST_AGENT_KEY = 'couch.lastAgent'

export function NewSessionPrompt() {
  // Hydration-safe: empty on first render, populated from localStorage after mount.
  const [agentId, setAgentId] = useState<string>('')
  const [message, setMessage] = useState('')
  const [pending, startTransition] = useTransition()

  useEffect(() => {
    const stored = localStorage.getItem(LAST_AGENT_KEY)
    if (stored) setAgentId(stored)
  }, [])

  const { data: agents = [] } = useQuery({
    queryKey: ['agents'],
    queryFn: listAgentsAction,
  })

  const canSubmit = agentId && message.trim().length > 0 && !pending

  function submit() {
    if (!canSubmit) return
    localStorage.setItem(LAST_AGENT_KEY, agentId)
    startTransition(async () => {
      await createSessionAction({ agent_id: agentId, message: message.trim() })
    })
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col items-center gap-6 px-4 py-16">
      <span aria-hidden className="text-6xl opacity-50">
        🛋️
      </span>
      <div className="w-full rounded-2xl border bg-card p-3 shadow-sm">
        <Textarea
          className="resize-none border-0 bg-transparent px-2 pt-1 text-base shadow-none focus-visible:ring-0"
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault()
              submit()
            }
          }}
          placeholder="Ask Couch to build features, fix bugs, or work on your code"
          rows={3}
          value={message}
        />
        <div className="mt-2 flex items-center justify-between gap-2">
          <Select
            onValueChange={(value) => setAgentId(value ?? '')}
            value={agentId || undefined}
          >
            <SelectTrigger aria-label="agent" className="h-9">
              <SelectValue placeholder="Pick an agent" />
            </SelectTrigger>
            <SelectContent>
              {agents.map((a) => (
                <SelectItem key={a.name} value={a.name}>
                  {a.display_name ?? a.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            aria-label="submit"
            className="h-9 w-9 rounded-full"
            disabled={!canSubmit}
            onClick={submit}
            size="icon"
          >
            <ArrowUp className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
