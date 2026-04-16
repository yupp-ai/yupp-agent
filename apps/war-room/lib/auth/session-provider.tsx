'use client'

import { createContext, useContext } from 'react'
import type { ClientSession } from './auth-types'

const SessionContext = createContext<ClientSession | null>(null)

export function useSession() {
  const session = useContext(SessionContext)
  if (!session) {
    throw new Error('useSession must be used within SessionProvider')
  }
  return session
}

export function SessionProvider({
  children,
  session,
}: {
  children: React.ReactNode
  session: ClientSession
}) {
  return (
    <SessionContext.Provider value={session}>
      {children}
    </SessionContext.Provider>
  )
}
