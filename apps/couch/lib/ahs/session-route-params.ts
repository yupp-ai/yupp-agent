const RESERVED_SESSION_ROUTE_IDS = new Set(['new'])

function normalizeSessionRouteId(id: string): string {
  return id.trim()
}

export function isSessionRouteIdValid(id: string): boolean {
  const normalizedId = normalizeSessionRouteId(id)

  return (
    normalizedId.length > 0 && !RESERVED_SESSION_ROUTE_IDS.has(normalizedId)
  )
}

export function getSideBySideSessionIds(params: {
  left: string
  right: string
}): { left: string; right: string } | null {
  const left = normalizeSessionRouteId(params.left)
  const right = normalizeSessionRouteId(params.right)

  if (!isSessionRouteIdValid(left) || !isSessionRouteIdValid(right)) {
    return null
  }

  if (left === right) {
    return null
  }

  return { left, right }
}
