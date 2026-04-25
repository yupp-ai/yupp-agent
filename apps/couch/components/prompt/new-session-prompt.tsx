'use client'

import { useQuery } from '@tanstack/react-query'
import { Select as SelectPrimitive } from '@base-ui/react/select'
import { ArrowUp, ChevronDown } from 'lucide-react'
import { useEffect, useMemo, useState, useTransition } from 'react'
import { createSessionAction } from '@/app/_actions/create-session'
import { listAgentsAction } from '@/app/_actions/list-agents'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

const LAST_AGENT_KEY = 'couch.lastAgent'
const DEFAULT_AGENT = 'eng-raccoon'

export function NewSessionPrompt() {
  // Hydration-safe: empty on first render, populated from localStorage after mount.
  const [agentId, setAgentId] = useState<string>('')
  const [message, setMessage] = useState('')
  const [pending, startTransition] = useTransition()

  const { data: agents = [] } = useQuery({
    queryKey: ['agents'],
    queryFn: listAgentsAction,
  })

  const options = useMemo(
    () =>
      agents.map((a) => ({
        label: a.display_name ?? a.name,
        value: a.name,
      })),
    [agents]
  )

  // Resolve default once we know the agent list and localStorage value.
  useEffect(() => {
    if (agentId) return
    const stored = localStorage.getItem(LAST_AGENT_KEY) ?? ''
    const fromStorage = options.find((o) => o.value === stored)?.value
    if (fromStorage) {
      setAgentId(fromStorage)
      return
    }
    const fallback = options.find((o) => o.value === DEFAULT_AGENT)?.value
    if (fallback) setAgentId(fallback)
  }, [options, agentId])

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
            // Enter sends; Cmd/Ctrl-Enter or Shift-Enter inserts a newline.
            if (
              e.key === 'Enter' &&
              !e.metaKey &&
              !e.ctrlKey &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault()
              submit()
            }
          }}
          placeholder="Ask Couch anything"
          rows={3}
          value={message}
        />
        <div className="mt-2 flex items-center justify-between gap-2 px-1">
          <AgentSelect
            options={options}
            value={agentId}
            onChange={setAgentId}
          />
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

function AgentSelect({
  options,
  value,
  onChange,
}: {
  options: { label: string; value: string }[]
  value: string
  onChange: (next: string) => void
}) {
  const selected = options.find((o) => o.value === value)
  return (
    <SelectPrimitive.Root
      items={options}
      onValueChange={(next) => onChange((next ?? '') as string)}
      value={value}
    >
      <SelectPrimitive.Trigger
        className={cn(
          'flex h-9 items-center gap-1.5 rounded-full border bg-background px-3 text-sm text-foreground outline-none transition-colors',
          'hover:bg-accent focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40'
        )}
      >
        <SelectPrimitive.Value
          className="truncate"
          placeholder="Pick an agent"
        >
          {selected?.label ?? 'Pick an agent'}
        </SelectPrimitive.Value>
        <SelectPrimitive.Icon aria-hidden className="text-muted-foreground">
          <ChevronDown className="h-4 w-4" />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>

      <SelectPrimitive.Portal>
        <SelectPrimitive.Positioner
          align="start"
          alignItemWithTrigger={false}
          className="z-50"
          sideOffset={4}
        >
          <SelectPrimitive.Popup className="max-h-80 min-w-[var(--anchor-width)] overflow-hidden rounded-xl border bg-popover shadow-lg">
            <SelectPrimitive.List className="max-h-80 overflow-y-auto p-1">
              {options.map((option) => (
                <SelectPrimitive.Item
                  className={(state) =>
                    cn(
                      'flex cursor-default items-center rounded-md px-2 py-1.5 text-sm outline-none',
                      state.highlighted && 'bg-accent',
                      state.selected && 'bg-accent/70 font-medium'
                    )
                  }
                  key={option.value}
                  value={option.value}
                >
                  <SelectPrimitive.ItemText className="truncate">
                    {option.label}
                  </SelectPrimitive.ItemText>
                </SelectPrimitive.Item>
              ))}
            </SelectPrimitive.List>
          </SelectPrimitive.Popup>
        </SelectPrimitive.Positioner>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  )
}
