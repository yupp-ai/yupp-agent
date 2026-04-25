'use client'

import { useQuery } from '@tanstack/react-query'
import { ArrowUp } from 'lucide-react'
import { useState, useTransition } from 'react'
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
  const [agentId, setAgentId] = useState<string>(() =>
    typeof window === 'undefined'
      ? ''
      : (localStorage.getItem(LAST_AGENT_KEY) ?? '')
  )
  const [message, setMessage] = useState('')
  const [pending, startTransition] = useTransition()

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
      <div className="w-full rounded-2xl border bg-card shadow-sm">
        <Textarea
          className="resize-none border-0 px-4 pt-4 text-base shadow-none focus-visible:ring-0"
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
        <div className="flex items-center justify-between px-3 pb-3">
          <Select
            onValueChange={(value) => setAgentId(value ?? '')}
            value={agentId}
          >
            <SelectTrigger aria-label="agent" className="h-8 w-44">
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
            disabled={!canSubmit}
            onClick={submit}
            size="sm"
          >
            <ArrowUp className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
