import type { ReactNode } from "react";

/**
 * Shared layout primitives extracted from the original
 * monolithic SettingsDrawer.tsx during the file-size refactor
 * (see AGENTS.md "File size guidance"). Every settings tab uses
 * these so they live in one place at the root of the
 * `web/src/features/settings/` folder.
 */

// Re-exported from the shared lib so the Memory-tab panels keep their
// existing ``import { formatRelative } from "../SettingsSection"`` while
// the single implementation lives in one place.
export { formatRelative } from "@/lib/time";

export function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-100/50">
        {title}
      </h3>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

export function Row({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between rounded-md bg-white/[0.02] px-3 py-1.5 text-[11px] text-ink-100/60">
      <span>{label}</span>
      <span className="font-mono text-ink-100/80">{value}</span>
    </div>
  );
}