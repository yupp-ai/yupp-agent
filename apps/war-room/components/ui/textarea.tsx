import type * as React from 'react'
import { cn } from '@/components/ui/utils'

function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return (
    <textarea
      className={cn(
        'min-h-[120px] rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500',
        className
      )}
      data-slot="textarea"
      {...props}
    />
  )
}

export { Textarea }
