'use client'

import { ArrowUp } from 'lucide-react'
import { useState, useTransition } from 'react'
import { sendMessageAction } from '@/app/_actions/send-message'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

export function FollowUpPrompt({ sessionId }: { sessionId: string }) {
  const [message, setMessage] = useState('')
  const [pending, startTransition] = useTransition()

  function submit() {
    const trimmed = message.trim()
    if (!trimmed || pending) return
    startTransition(async () => {
      await sendMessageAction({ sessionId, message: trimmed })
      setMessage('')
    })
  }

  return (
    <div className="border-t bg-background px-4 py-3">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
        <Textarea
          className="resize-none"
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
        <div className="flex justify-end">
          <Button
            aria-label="send"
            disabled={!message.trim() || pending}
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
