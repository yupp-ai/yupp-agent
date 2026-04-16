import { fetchRequestHandler } from '@trpc/server/adapters/fetch'
import { appActions } from '@/server/trpc/actions'
import { createRouteContext } from '@/server/trpc/init'

const handler = (req: Request) =>
  fetchRequestHandler({
    endpoint: '/api/trpc',
    req,
    router: appActions,
    createContext: createRouteContext,
  })

export { handler as GET, handler as POST }
