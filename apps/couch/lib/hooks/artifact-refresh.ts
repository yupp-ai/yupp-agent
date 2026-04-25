/**
 * How often to refetch the artifact list. Kept tight while a turn is in
 * flight so freshly-produced artifacts surface within a few seconds; loose
 * otherwise so we're not hammering AHS for stable sessions.
 *
 * v2 will replace this with WS push events ("artifact/created") emitted
 * by the AHS artifact_store, at which point the polling becomes a safety
 * net rather than the primary signal.
 */
export function resolveArtifactRefetchInterval(turnInFlight: boolean): number {
  return turnInFlight ? 5_000 : 30_000
}
