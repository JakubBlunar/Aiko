/**
 * Three-tone fill meter (emerald < 0.6, amber < 0.85, rose otherwise).
 * Extracted from the duplicated context-fill bars in ContextBadge and
 * DiagnosticsSection.
 */
export function MeterBar({ pct }: { pct: number }) {
  const clamped = Math.max(0, Math.min(1, pct));
  const fill = Math.round(clamped * 100);
  const tone =
    clamped < 0.6
      ? "bg-emerald-400"
      : clamped < 0.85
        ? "bg-amber-400"
        : "bg-rose-500";
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-white/10">
      <div className={`h-full ${tone}`} style={{ width: `${fill}%` }} />
    </div>
  );
}
