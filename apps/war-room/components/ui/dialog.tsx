'use client'

import { Dialog as DialogPrimitive } from '@base-ui/react/dialog'
import * as React from 'react'
import { cn } from '@/components/ui/utils'

const Dialog = DialogPrimitive.Root
const DialogTrigger = DialogPrimitive.Trigger
const DialogClose = DialogPrimitive.Close

const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    className={cn('font-medium text-base text-zinc-100', className)}
    ref={ref}
    {...props}
  />
))
DialogTitle.displayName = 'DialogTitle'

const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    className={cn('text-sm text-zinc-400', className)}
    ref={ref}
    {...props}
  />
))
DialogDescription.displayName = 'DialogDescription'

const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Popup>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Popup> & {
    showCloseButton?: boolean
  }
>(({ children, className, showCloseButton = true, ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogPrimitive.Backdrop className="fixed inset-0 z-40 bg-zinc-950/5" />
    <div className="fixed inset-0 z-50 overflow-y-auto bg-zinc-950/20 supports-[backdrop-filter]:bg-zinc-950/6 supports-[backdrop-filter]:backdrop-blur-lg supports-[backdrop-filter]:backdrop-saturate-110">
      <div className="flex min-h-full items-start justify-center p-4 sm:items-center">
        <DialogPrimitive.Popup
          className={cn(
            'relative w-full max-w-4xl rounded-2xl border border-zinc-800 bg-zinc-950 p-6 shadow-2xl outline-none',
            className
          )}
          ref={ref}
          {...props}
        >
          {showCloseButton && (
            <DialogPrimitive.Close
              aria-label="Close dialog"
              className="absolute top-4 right-4 rounded-md px-2 py-1 text-sm text-zinc-400 transition-colors hover:text-zinc-100 focus:outline-none focus:ring-2 focus:ring-zinc-500"
            >
              Close
            </DialogPrimitive.Close>
          )}
          {children}
        </DialogPrimitive.Popup>
      </div>
    </div>
  </DialogPrimitive.Portal>
))
DialogContent.displayName = 'DialogContent'

function DialogHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('flex flex-col gap-1.5', className)} {...props} />
}

function DialogFooter({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'flex flex-col-reverse gap-2 sm:flex-row sm:justify-end',
        className
      )}
      {...props}
    />
  )
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
}
