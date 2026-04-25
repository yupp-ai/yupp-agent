'use client'

import { ArrowUp } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

export function FollowUpPrompt({
  onSendMessage,
}: {
  sessionId: string
  onSendMessage: (content: string) => void
}) {
  const [message, setMessage] = useState('')

  function submit() {
    const trimmed = message.trim()
    if (!trimmed) return
    onSendMessage(trimmed)
    setMessage('')
  }

  return (
    <div className="border-t bg-background px-4 py-3">
      <div className="mx-auto flex w-full max-w-3xl items-end gap-2">
        <Textarea
          className="flex-1 resize-none"
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
        <Button
          aria-label="send"
          className="h-9 w-9 shrink-0 rounded-full"
          disabled={!message.trim()}
          onClick={submit}
          size="icon"
        >
          <ArrowUp className="h-4 w-4" />
        </Button>
      </div>
    </div>
  )
}
