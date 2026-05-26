/**
 * Shared easing helpers used by multiple channels. Lifted from the
 * legacy ``Live2DAvatar.tsx`` so a single source of truth is shared
 * across the engine + tests.
 */

/** Critically-damped easing toward a target value.
 *
 * ``rate`` is in "factor per second" — multiply by ``dt`` before
 * passing to get a frame-rate-independent ease. ``approach(0, 1, dt
 * / 0.5)`` reaches roughly 86% of 1 in 500ms, irrespective of the
 * frame interval.
 *
 * Returns ``current`` unchanged when ``rate <= 0`` so a paused
 * channel can pass ``0`` without explicit guards. */
export function approach(current: number, target: number, rate: number): number {
  if (rate <= 0) {
    return current;
  }
  const factor = 1 - Math.exp(-rate);
  return current + (target - current) * factor;
}

/** Clamp ``value`` to ``[lo, hi]``. */
export function clamp(value: number, lo: number, hi: number): number {
  if (value < lo) {
    return lo;
  }
  if (value > hi) {
    return hi;
  }
  return value;
}
