import type { ReactNode } from "react";

/** Muted "nothing here yet" paragraph shared by the Memory-tab panels. */
export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="text-[11px] text-ink-100/40">{children}</p>;
}
