/** I6: "Load older messages" affordance pinned to the top of the chat
 * scroll. Renders nothing once we've paged back to the start of the
 * conversation (``hasMore === false``) so it never lingers as a dead
 * button. */
export function LoadOlderHeader({
  hasMore,
  loading,
  onLoad,
}: {
  hasMore: boolean;
  loading: boolean;
  onLoad: () => void;
}) {
  if (!hasMore) {
    return <div className="pt-8" />;
  }
  return (
    <div className="flex justify-center px-6 pb-2 pt-8">
      <button
        type="button"
        onClick={onLoad}
        disabled={loading}
        className="rounded-full border border-white/10 bg-white/[0.04] px-4 py-1.5 text-xs text-ink-100/70 transition hover:bg-white/10 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {loading ? "Loading…" : "Load older messages"}
      </button>
    </div>
  );
}
