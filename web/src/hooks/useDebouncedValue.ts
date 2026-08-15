import { useEffect, useState } from "react";

/**
 * Trails `value` by `delayMs`, resetting the timer on every change.
 *
 * For search boxes whose value is a server query parameter: typing
 * "bottle cap" would otherwise fire ten requests, and because responses
 * can land out of order the list would settle on whichever one was
 * slowest rather than the one matching what is in the box.
 *
 * The first value passes through immediately, so a panel mounting with a
 * restored query does not flash an unfiltered list first.
 */
export function useDebouncedValue<T>(value: T, delayMs = 250): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    if (settled === value) return;
    const timer = window.setTimeout(() => setSettled(value), delayMs);
    return () => window.clearTimeout(timer);
    // `settled` is deliberately not a dependency: including it would
    // restart the timer when it lands, and the guard above already makes
    // the effect a no-op once the two agree.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, delayMs]);

  return settled;
}
