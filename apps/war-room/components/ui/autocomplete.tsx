'use client'

import { Autocomplete as AutocompletePrimitive } from '@base-ui/react/autocomplete'
import type * as React from 'react'
import { cn } from '@/components/ui/utils'

type AutocompleteProps = Omit<
  AutocompletePrimitive.Root.Props<string>,
  'children' | 'items' | 'onValueChange' | 'value' | 'defaultValue'
> & {
  options: string[]
  value?: string
  defaultValue?: string
  onValueChange?: (value: string) => void
  className?: string
  listClassName?: string
  placeholder?: string
  emptyMessage?: React.ReactNode
}

function Autocomplete({
  options,
  value,
  defaultValue,
  onValueChange,
  className,
  listClassName,
  placeholder,
  emptyMessage = 'No results found.',
  ...props
}: AutocompleteProps) {
  return (
    <AutocompletePrimitive.Root
      items={options}
      onValueChange={(nextValue) => onValueChange?.(nextValue)}
      {...(value !== undefined ? { value } : {})}
      {...(defaultValue !== undefined ? { defaultValue } : {})}
      {...props}
    >
      <AutocompletePrimitive.Input
        className={cn(
          'rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-100 placeholder:text-zinc-500',
          className
        )}
        data-slot="autocomplete-input"
        placeholder={placeholder}
      />

      <AutocompletePrimitive.Portal>
        <AutocompletePrimitive.Positioner className="z-50" sideOffset={4}>
          <AutocompletePrimitive.Popup
            className="max-h-80 min-w-[var(--anchor-width)] overflow-hidden rounded-md border border-zinc-700 bg-zinc-900 shadow-lg"
            data-slot="autocomplete-popup"
          >
            <AutocompletePrimitive.List
              className={cn('max-h-80 overflow-y-auto p-1', listClassName)}
              data-slot="autocomplete-list"
            >
              {(item: string, index: number) => (
                <AutocompletePrimitive.Item
                  className={(state) =>
                    cn(
                      'flex cursor-default items-center rounded px-2 py-1.5 text-sm text-zinc-100 outline-none',
                      state.highlighted && 'bg-zinc-800',
                      state.selected && 'bg-zinc-800/80',
                      state.disabled && 'cursor-not-allowed text-zinc-500'
                    )
                  }
                  key={`${item}-${index}`}
                  value={item}
                >
                  {item}
                </AutocompletePrimitive.Item>
              )}
            </AutocompletePrimitive.List>
            <AutocompletePrimitive.Empty className="px-3 py-2 text-sm text-zinc-400">
              {emptyMessage}
            </AutocompletePrimitive.Empty>
          </AutocompletePrimitive.Popup>
        </AutocompletePrimitive.Positioner>
      </AutocompletePrimitive.Portal>
    </AutocompletePrimitive.Root>
  )
}

export { Autocomplete }
