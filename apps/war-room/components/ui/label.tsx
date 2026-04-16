import { cva, type VariantProps } from 'class-variance-authority'
import type * as React from 'react'
import { cn } from '@/components/ui/utils'

const labelVariants = cva('flex flex-col gap-1', {
  variants: {
    size: {
      default: 'text-sm text-zinc-300',
      sm: 'text-xs text-zinc-400',
    },
  },
  defaultVariants: {
    size: 'default',
  },
})

type LabelProps = React.ComponentProps<'label'> &
  VariantProps<typeof labelVariants>

function Label({ className, size, ...props }: LabelProps) {
  return (
    <label
      className={cn(labelVariants({ size }), className)}
      data-slot="label"
      {...props}
    />
  )
}

export { Label, labelVariants }
