import { type ReactNode, useEffect, useRef } from "react";

export interface TabStripItem<T extends string> {
  id: T;
  label: string;
  icon?: ReactNode;
}

/**
 * Horizontally-scrollable tab bar. Extracted from the SettingsDrawer nav so
 * the active-pill styling + the vertical-wheel-scrolls-horizontally
 * behaviour live in one place. Generic over the tab-id union so ``onSelect``
 * stays type-safe at the call site.
 */
export function TabStrip<T extends string>({
  tabs,
  activeId,
  onSelect,
  ariaLabel,
}: {
  tabs: ReadonlyArray<TabStripItem<T>>;
  activeId: T;
  onSelect: (id: T) => void;
  ariaLabel?: string;
}) {
  const barRef = useRef<HTMLElement | null>(null);

  // Let a vertical wheel scroll the horizontal tab bar. A React onWheel is
  // passive (preventDefault is a no-op), so attach a native non-passive
  // listener. The component only mounts while its host surface is open, so a
  // mount-time attach is equivalent to the old ``open``-gated effect.
  useEffect(() => {
    const el = barRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY === 0) return;
      el.scrollLeft += e.deltaY;
      e.preventDefault();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  return (
    <nav
      ref={barRef}
      className="flex shrink-0 gap-1 overflow-x-auto border-b border-white/5 bg-white/[0.015] px-3 py-2"
      aria-label={ariaLabel}
    >
      {tabs.map((tab) => {
        const isActive = activeId === tab.id;
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onSelect(tab.id)}
            aria-pressed={isActive}
            className={`flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition ${
              isActive
                ? "bg-ink-500/30 text-ink-100 ring-1 ring-ink-400/50"
                : "text-ink-100/60 hover:bg-white/5 hover:text-ink-100/90"
            }`}
          >
            {tab.icon != null ? (
              <span aria-hidden="true">{tab.icon}</span>
            ) : null}
            <span>{tab.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
