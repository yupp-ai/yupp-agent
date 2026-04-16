import type * as React from 'react'
import { cn } from '@/components/ui/utils'

function Skeleton({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn('animate-pulse rounded-md bg-zinc-800', className)}
      data-slot="skeleton"
      {...props}
    />
  )
}

export { Skeleton }
