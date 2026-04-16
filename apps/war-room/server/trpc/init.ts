import { initTRPC, TRPCError } from '@trpc/server'
import { headers } from 'next/headers'
import { cache } from 'react'
import { canAccessWarRoom } from '@/lib/auth/authorization'
import { getInternalSession } from '@/lib/auth/get-session'
import { getIpAddressFromHeader } from '@/lib/requests'

export const createRouteContext = cache(async () => {
  const session = await getInternalSession()
  const headerList = await headers()

  return {
    session,
    ip: getIpAddressFromHeader(headerList) || 'unknown',
    userAgent: headerList.get('user-agent') || 'unknown',
    hostname: headerList.get('host') || undefined,
  }
})

type TrpcContext = Awaited<ReturnType<typeof createRouteContext>>

const t = initTRPC.context<TrpcContext>().create()

export const trpcActions = t.router
export const publicAction = t.procedure

export const protectedAction = t.procedure.use(async ({ ctx, next }) => {
  if (ctx.session.status === 'unauthenticated') {
    throw new TRPCError({ code: 'UNAUTHORIZED' })
  }

  return next({
    ctx: {
      ...ctx,
      session: ctx.session,
    },
  })
})

export const operatorAction = protectedAction.use(async ({ ctx, next }) => {
  if (!canAccessWarRoom(ctx.session.user)) {
    throw new TRPCError({ code: 'FORBIDDEN' })
  }

  return next()
})
