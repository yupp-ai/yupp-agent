import { ahsActions } from './ahs-actions'
import { trpcActions } from './init'
import { slackGatewayActions } from './slack-gateway-actions'

export const appActions = trpcActions({
  ahs: ahsActions,
  slackGateway: slackGatewayActions,
})

export type AppActions = typeof appActions
