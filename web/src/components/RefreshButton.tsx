/**
 * The small bordered "refresh" button shared by every Memory-tab panel.
 * Doubles as the "reflect now" / "regenerate now" action button: pass a
 * custom `label` and a combined busy flag as `loading` (renders "..."
 * and disables while busy).
 */
export function RefreshButton({
  onClick,
  loading,
  label = "refresh",
  title,
}: {
  onClick: () => void;
  loading: boolean;
  label?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={loading}
      title={title}
      className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
    >
      {loading ? "..." : label}
    </button>
  );
}
