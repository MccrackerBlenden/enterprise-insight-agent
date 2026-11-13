/** Monotonic read guard. A cancelled/invalidated response must not mutate UI state. */
export function createRequestEpoch() {
  let revision = 0
  return {
    begin: () => ++revision,
    invalidate: () => { revision += 1 },
    isCurrent: (ticket: number) => ticket === revision,
  }
}

/** A user-initiated selection wins over a slower refresh with an old destination. */
export function canRestoreRefreshSelection(
  selectedAtRequest: number, currentSelectionRevision: number, requestedId?: string,
): boolean {
  return Boolean(requestedId) && selectedAtRequest === currentSelectionRevision
}
