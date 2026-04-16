'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { cn } from '@/components/ui/utils'

const NAV_LINKS = [
  { href: '/', label: 'Home' },
  { href: '/sessions', label: 'Sessions' },
  { href: '/projects', label: 'Projects' },
  { href: '/agents', label: 'Agents' },
  { href: '/schedules', label: 'Automations' },
]

export function NavLinks() {
  const pathname = usePathname()

  return (
    <>
      {NAV_LINKS.map((link) => {
        const isActive =
          link.href === '/' ? pathname === '/' : pathname.startsWith(link.href)

        return (
          <Link
            className={cn(
              'rounded-md px-2.5 py-1 text-xs transition-colors',
              isActive
                ? 'bg-zinc-800 text-zinc-100'
                : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100'
            )}
            href={link.href}
            key={link.href}
          >
            {link.label}
          </Link>
        )
      })}
    </>
  )
}
