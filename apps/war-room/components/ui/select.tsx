'use client'

import { Select as SelectPrimitive } from '@base-ui/react/select'
import type * as React from 'react'
import { cn } from '@/components/ui/utils'

type SelectOption = {
  label: React.ReactNode
  value: string
  disabled?: boolean
}

type SelectProps = Omit<
  SelectPrimitive.Root.Props<string>,
  'children' | 'items' | 'onValueChange' | 'value' | 'defaultValue'
> & {
  options: SelectOption[]
  value?: string
  defaultValue?: string
  onValueChange?: (value: string) => void
  className?: string
  listClassName?: string
  placeholder?: React.ReactNode
}

function Select({
  options,
  value,
  defaultValue,
  onValueChange,
  className,
  listClassName,
  placeholder,
  ...props
}: SelectProps) {
  return (
    <SelectPrimitive.Root
      items={options}
      onValueChange={(nextValue) =>
        onValueChange?.((nextValue ?? '') as string)
      }
      {...(value !== undefined ? { value } : {})}
      {...(defaultValue !== undefined ? { defaultValue } : {})}
      {...props}
    >
      <SelectPrimitive.Trigger
        className={cn(
          'flex w-full items-center justify-between gap-2 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-left text-sm text-zinc-100 outline-none transition-colors focus-visible:border-zinc-500 disabled:opacity-50',
          className
        )}
        data-slot="select-trigger"
      >
        <SelectPrimitive.Value className="truncate" placeholder={placeholder} />
        <SelectPrimitive.Icon aria-hidden className="text-zinc-400">
          <span>▾</span>
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>

      <SelectPrimitive.Portal>
        <SelectPrimitive.Positioner
          align="start"
          alignItemWithTrigger={false}
          className="z-50"
          sideOffset={4}
        >
          <SelectPrimitive.Popup
            className="max-h-80 min-w-[var(--anchor-width)] overflow-hidden rounded-md border border-zinc-700 bg-zinc-900 shadow-lg"
            data-slot="select-popup"
          >
            <SelectPrimitive.List
              className={cn('max-h-80 overflow-y-auto p-1', listClassName)}
              data-slot="select-list"
            >
              {options.map((option) => (
                <SelectPrimitive.Item
                  className={(state) =>
                    cn(
                      'flex cursor-default items-center rounded px-2 py-1.5 text-sm text-zinc-100 outline-none',
                      state.highlighted && 'bg-zinc-800',
                      state.selected && 'bg-zinc-800/80',
                      state.disabled && 'cursor-not-allowed text-zinc-500'
                    )
                  }
                  disabled={option.disabled}
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

export { Select, type SelectOption }
