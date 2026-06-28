/**
 * Monotonic client-side id generator for chat bubbles + toasts. Kept in
 * its own module because both the chat slice and the notifications slice
 * mint ids from the same counter, and ``clearMessages`` resets it.
 */
let bubbleCounter = 0;

export const nextId = (): string => {
  bubbleCounter += 1;
  return `m_${Date.now().toString(36)}_${bubbleCounter}`;
};

/** Reset the counter — called by ``clearMessages`` on a fresh session. */
export const resetIdCounter = (): void => {
  bubbleCounter = 0;
};
