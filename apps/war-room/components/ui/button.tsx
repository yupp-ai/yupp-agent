'use client'

import { Button as ButtonPrimitive } from '@base-ui/react/button'
import { cva, type VariantProps } from 'class-variance-authority'
import type * as React from 'react'
import { cn } from '@/components/ui/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center rounded-md border text-sm transition-colors disabled:opacity-50',
  {
    variants: {
      variant: {
        default: 'border-zinc-700 bg-zinc-900 text-zinc-100',
        primary: 'border-blue-700/60 bg-blue-900/30 text-blue-100',
        destructive: 'border-red-700/60 bg-red-900/30 text-red-100',
      },
      size: {
        default: 'px-3 py-1.5',
        sm: 'px-2 py-1 text-xs',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
)

type ButtonProps = Omit<
  React.ComponentProps<typeof ButtonPrimitive>,
  'className'
> &
  VariantProps<typeof buttonVariants> & { className?: string }

function Button({ className, variant, size, ...props }: ButtonProps) {
  return (
    <ButtonPrimitive
      className={cn(buttonVariants({ variant, size }), className)}
      data-slot="button"
      {...props}
    />
  )
}

export { Button, buttonVariants }
