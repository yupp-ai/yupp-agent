'use client'

import { useEffect, useState } from 'react'

const FRAMES = ['-', '/', '|', '\\', '-', '*']

/**
 * Simple "still working" placeholder rendered at the end of the feed while
 * a turn is in flight. The animated frame keeps cycling so the user always
 * sees motion, even between streamed events. New tool/assistant items push
 * this indicator down naturally because we render it after them.
 */
export function ThinkingIndicator() {
  const [frame, setFrame] = useState(0)

  useEffect(() => {
    const id = setInterval(() => {
      setFrame((f) => (f + 1) % FRAMES.length)
    }, 120)
    return () => clearInterval(id)
  }, [])

  return (
    <div
      aria-label="thinking"
      className="flex items-center gap-2 px-1 py-3 text-muted-foreground text-sm"
      role="status"
    >
      <span
        aria-hidden
        className="inline-block w-3 text-center font-mono text-foreground"
      >
        {FRAMES[frame]}
      </span>
      <span>Thinking…</span>
    </div>
  )
}
