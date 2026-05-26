/**
 * Shared mutable state across channels.
 *
 * Most channel state is private. The fields below are the *handful*
 * that genuinely need cross-channel coordination — keeping them on a
 * tiny shared object instead of a global event bus keeps the
 * coupling visible.
 *
 * Today's contents:
 *
 * - ``exprSlotLockUntil``: the ``performance.now()`` deadline of the
 *   active expression-bound overlay pulse. ``OverlayChannel`` writes
 *   it when firing ``model.expression(name)`` for an ``expr:`` binding;
 *   ``ExpressionChannel`` reads it to defer re-applying the
 *   persistent reaction expression (so a ``[[overlay:grin]]`` mid-turn
 *   isn't immediately clobbered by ``applyReaction``). When the
 *   overlay tick crosses the deadline the channel resets it to 0 and
 *   notifies the engine to re-apply the persistent reaction.
 *   Documented in ``docs/alexia-model-notes.md`` section 5.
 * - ``lastReaction``: the last reaction string the engine applied.
 *   Used for dedup so the same reaction across two consecutive
 *   turns doesn't fire ``model.expression`` twice.
 * - ``ttsState``: mirror of the store's TTS state. Channels that
 *   change behaviour while speaking (gaze lock, talk-motion start)
 *   read this on every relevant tick.
 *
 * New shared state lands here as channels are migrated. Anything
 * that doesn't need cross-channel reads stays inside the channel
 * that owns it.
 */
export interface EngineState {
  exprSlotLockUntil: number;
  lastReaction: string;
  ttsState: "idle" | "speaking";
  /** Monotonic deadline (``performance.now()``) of an active
   * ``[[overlay:tail_wag]]`` boost. ``0`` when no boost is active.
   * GestureChannel sets it; AmbientBodyChannel reads it to scale
   * the always-on cat-tail sine. Splitting "set" and "read" across
   * channels keeps each side single-purpose: GestureChannel owns
   * the discrete LLM-event, AmbientBodyChannel owns the per-frame
   * sine drive. */
  tailWagBoostUntil: number;
}

export function createEngineState(): EngineState {
  return {
    exprSlotLockUntil: 0,
    lastReaction: "",
    ttsState: "idle",
    tailWagBoostUntil: 0,
  };
}
