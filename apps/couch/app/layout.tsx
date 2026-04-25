import type { Metadata } from 'next'
import { Geist, Geist_Mono, Inter } from 'next/font/google'
import { Suspense } from 'react'
import { Providers } from '@/components/providers'
import { getSession } from '@/lib/auth/get-session'
import { SessionExpirationNotice } from '@/lib/auth/session-expiration-notice'
import { SessionProvider } from '@/lib/auth/session-provider'
import { LoginScreen } from './login-screen'
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
  title: 'Couch',
  description: 'AHS web frontend.',
}

function AuthShellFallback() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-md rounded-2xl border bg-card p-8 shadow-sm">
        <p className="text-3xl">🛋️</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Couch</h1>
        <p className="mt-1 text-sm text-muted-foreground">Loading session…</p>
      </div>
    </div>
  )
}

async function AuthenticatedApp({
  children,
}: Readonly<{ children: React.ReactNode }>) {
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
      <SessionExpirationNotice />
      <Providers>{children}</Providers>
    </SessionProvider>
  )
}

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html className={inter.variable} lang="en">
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        <Suspense fallback={<AuthShellFallback />}>
          <AuthenticatedApp>{children}</AuthenticatedApp>
        </Suspense>
      </body>
    </html>
  )
}
