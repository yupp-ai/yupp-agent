import type { RefObject } from 'react'
import { useEffect } from 'react'

function focusTextarea(textarea: HTMLTextAreaElement | null) {
  if (!textarea || textarea.disabled || textarea.readOnly) {
    return
  }

  textarea.focus()
}

function isSpecialKeyCode(keyCode: number): boolean {
  return (
    // Tab, Enter, Escape, Arrow keys
    (keyCode >= 9 && keyCode <= 13) ||
    keyCode === 27 ||
    (keyCode >= 37 && keyCode <= 40) ||
    // Home, End, Page Up, Page Down, Insert, Delete
    (keyCode >= 33 && keyCode <= 36) ||
    keyCode === 45 ||
    keyCode === 46 ||
    // F1-F12
    (keyCode >= 112 && keyCode <= 123) ||
    // Other special keys
    keyCode === 144 || // Num Lock
    keyCode === 145 || // Scroll Lock
    keyCode === 20 // Caps Lock
  )
}

function shouldIgnoreTarget(target: HTMLElement): boolean {
  return (
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.tagName === 'SELECT' ||
    target.isContentEditable ||
    Boolean(
      target.closest('[data-slot="select-popup"], [data-slot="select-trigger"]')
    )
  )
}

export function useAutofocusOnKeyPress(
  textareaRef: RefObject<HTMLTextAreaElement | null>,
  isEnabled: boolean = true
) {
  useEffect(() => {
    if (!isEnabled) {
      return
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) {
        return
      }

      if (
        event.key === 'Shift' ||
        event.key === 'Control' ||
        event.key === 'Alt' ||
        event.key === 'Meta'
      ) {
        return
      }

      const target = event.target

      if (!(target instanceof HTMLElement)) {
        return
      }

      if (
        shouldIgnoreTarget(target) ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        isSpecialKeyCode(event.keyCode)
      ) {
        return
      }

      focusTextarea(textareaRef.current)
    }

    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [isEnabled, textareaRef])
}
