import type * as React from 'react'
import { cn } from '@/components/ui/utils'

function Badge({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn(
        'rounded-md bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300',
        className
      )}
      data-slot="badge"
      {...props}
    />
  )
}

export { Badge }
