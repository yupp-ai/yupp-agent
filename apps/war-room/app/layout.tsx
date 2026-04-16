import type { Metadata } from 'next'
import { Geist, Geist_Mono, Inter } from 'next/font/google'
import { Suspense } from 'react'
import { getSession } from '@/lib/auth/get-session'
import { SessionExpirationNotice } from '@/lib/auth/session-expiration-notice'
import { SessionProvider } from '@/lib/auth/session-provider'
import { TRPCProvider } from '@/lib/hooks/trpc-client'
import { LoginScreen } from './login-screen'
import { NavLinks } from './nav-links'
import './globals.css'

const inter = Inter({ subsets: ['latin'], variable: '--font-sans' })

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
})

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
})

export const metadata: Metadata = {
  title: 'War Room',
  description: 'Create, control and monitor background agents.',
}

function AuthShellFallback() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-950 px-4">
      <div className="w-full max-w-md rounded-3xl border border-zinc-800 bg-zinc-900/70 p-8 shadow-2xl shadow-black/30">
        <p className="font-mono text-xs uppercase tracking-[0.28em] text-zinc-500">
          Yupp Internal
        </p>
        <h1 className="mt-3 font-bold text-3xl tracking-tight text-zinc-50">
          War Room
        </h1>
        <p className="mt-3 text-sm text-zinc-400">Loading session…</p>
      </div>
    </div>
  )
}

async function AuthenticatedApp({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  const session = await getSession()

  if (session.status === 'unauthenticated') {
    return <LoginScreen />
  }

  return (
    <SessionProvider
      session={{
        expiresAt: session.expiresAt,
        user: {
          id: session.user.id,
          firstName: session.user.firstName,
        },
      }}
    >
      <nav className="sticky top-0 z-50 flex items-center gap-1 border-b border-zinc-800 bg-zinc-950 px-4 py-2">
        <span className="mr-3 font-bold text-sm tracking-tight text-zinc-100 whitespace-nowrap">
          War Room
        </span>
        <Suspense>
          <NavLinks />
        </Suspense>
        <div className="ml-auto flex items-center gap-3">
          <span className="hidden text-xs text-zinc-500 md:block">
            {session.user.email}
          </span>
          <form
            action="/api/authentication/logout"
            className="hidden sm:block"
            method="post"
          >
            <button
              className="rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-100 transition-colors hover:bg-zinc-800"
              type="submit"
            >
              Sign out
            </button>
          </form>
        </div>
      </nav>
      <SessionExpirationNotice />
      <TRPCProvider>{children}</TRPCProvider>
    </SessionProvider>
  )
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html className={`${inter.variable} dark`} lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <Suspense fallback={<AuthShellFallback />}>
          <AuthenticatedApp>{children}</AuthenticatedApp>
        </Suspense>
      </body>
    </html>
  )
}
