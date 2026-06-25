import { useEffect, useState } from "react";

/** Viewport width (px) at/below which we switch to the phone layout.
 * Matches Tailwind's ``md`` breakpoint so the desktop layout is
 * untouched at >= 768px (tablets + laptops keep the sidebar + inline
 * avatar rail). */
export const MOBILE_MAX_WIDTH = 767;

const QUERY = `(max-width: ${MOBILE_MAX_WIDTH}px)`;

function matches(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(QUERY).matches;
}

/**
 * ``true`` when the viewport is phone-sized. Subscribes to the
 * ``matchMedia`` change event so the layout flips live on rotation /
 * window resize without a reload. SSR / test-node safe (returns
 * ``false`` when ``matchMedia`` is unavailable).
 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(matches);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(QUERY);
    const onChange = () => setIsMobile(mql.matches);
    // Sync once on mount in case the width changed between the initial
    // render and the effect firing.
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return isMobile;
}
