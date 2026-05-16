// Edge-safe constants extracted from session-cookie.ts so middleware
// (which runs on the edge runtime) doesn't pull in node:crypto.

import { isLocalDevelopment, isProduction } from './environments'

const useSecureCookie = !isLocalDevelopment
const nonProductionSuffix = isProduction ? '' : '-non-prod'

export const SESSION_COOKIE_NAME = useSecureCookie
  ? `__Secure-yupp.session-token${nonProductionSuffix}`
  : `yupp.session-token${nonProductionSuffix}`
