// Invalidation only: closed settings panels do not fetch plugin state.
const listeners = new Set<() => void>();
export function notifyPluginsUpdated() { for (const listener of listeners) listener(); }
export function subscribePluginsUpdated(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
