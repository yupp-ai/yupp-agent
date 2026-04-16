import 'server-only'

import type { NextRequest } from 'next/server'
import { getIpAddressFromHeader } from '@/lib/requests'

export function extractMetadataFromRequest(request: NextRequest) {
  const headers = request.headers

  return {
    ip_address: getIpAddressFromHeader(headers),
    country_code: headers.get('x-vercel-ip-country') || null,
    user_agent: headers.get('user-agent') || null,
    city: headers.get('x-vercel-ip-city') || null,
  }
}
