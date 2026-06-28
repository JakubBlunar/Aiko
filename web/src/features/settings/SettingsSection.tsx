import type { ReactNode } from "react";

/**
 * Shared layout primitives extracted from the original
 * monolithic SettingsDrawer.tsx during the file-size refactor
 * (see AGENTS.md "File size guidance"). Every settings tab uses
 * these so they live in one place at the root of the
 * `web/src/components/settings/` folder.
 */

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

/**
 * Compact "X seconds/minutes/hours/days ago" formatter used by
 * the Memory-tab status panels (BeliefsPanel, FactCheckerStatusFooter,
 * CuriositySeedsPanel). `null` / unparseable input renders as
 * "never" so call sites don't need to guard.
 */
export function formatRelative(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "never";
  const delta = Math.max(0, (Date.now() - t) / 1000);
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}
