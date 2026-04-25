export interface SessionPaginationGuardInput {
  isFetching: boolean
  isLocked: boolean
  loadedCount: number
  totalCount: number
}

/**
 * Extracted as a pure helper so pagination gating behavior is explicit and
 * unit-testable (instead of being implicit inside a component callback).
 */
export function canStartSessionPageLoad({
  isFetching,
  isLocked,
  loadedCount,
  totalCount,
}: SessionPaginationGuardInput): boolean {
  if (isFetching || isLocked) return false
  return loadedCount < totalCount
}
