import type { ReactNode } from "react";

/**
 * Standard settings sub-panel shell (the rounded, faintly-bordered card
 * the Memory-tab panels share). Extra classes can be merged for the
 * disabled/empty text-tone variant.
 */
export function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3 ${className}`.trim()}
    >
      {children}
    </div>
  );
}
