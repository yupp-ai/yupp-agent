'use client'

import { Input as InputPrimitive } from '@base-ui/react/input'
import type * as React from 'react'
import { cn } from '@/components/ui/utils'

type InputProps = Omit<
  React.ComponentProps<typeof InputPrimitive>,
  'className'
> & {
  className?: string
}

function Input({ className, ...props }: InputProps) {
  return (
    <InputPrimitive
      className={cn(
        'rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-100 placeholder:text-zinc-500',
        className
      )}
      data-slot="input"
      {...props}
    />
  )
}

export { Input }
