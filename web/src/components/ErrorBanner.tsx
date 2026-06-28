import type { ReactNode } from "react";

/**
 * Rose error banner shared across the settings surfaces. `compact` is
 * the tight inline variant the Memory-tab panels use; the default is
 * the larger banner the full tabs use.
 */
export function ErrorBanner({
  children,
  compact = false,
}: {
  children: ReactNode;
  compact?: boolean;
}) {
  return (
    <div
      className={
        compact
          ? "rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200"
          : "rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200"
      }
    >
      {children}
    </div>
  );
}
