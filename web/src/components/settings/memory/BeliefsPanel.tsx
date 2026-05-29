import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../../api";
import type {
  Belief,
  BeliefKind,
  BeliefStatus,
  BeliefsResponse,
} from "../../../types";
import { formatRelative } from "../SettingsSection";

const BELIEF_STATUS_FILTERS: { id: BeliefStatus | "all"; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "contradicted", label: "Contradicted" },
  { id: "confirmed", label: "Confirmed" },
  { id: "stale", label: "Stale" },
  { id: "all", label: "All" },
];

export function BeliefsPanel() {
  const [data, setData] = useState<BeliefsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<BeliefStatus | "all">("active");
  const [kindFilter, setKindFilter] = useState<BeliefKind | "all">("all");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snapshot = await api.listBeliefs({
        limit: 100,
        kind: kindFilter === "all" ? undefined : kindFilter,
        status: statusFilter === "all" ? undefined : statusFilter,
      });
      setData(snapshot);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [kindFilter, statusFilter]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleContradict = useCallback(
    async (belief: Belief) => {
      try {
        await api.updateBelief(belief.id, { status: "contradicted" });
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const handleConfirm = useCallback(
    async (belief: Belief) => {
      try {
        await api.updateBelief(belief.id, { status: "confirmed" });
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const handleDelete = useCallback(
    async (belief: Belief) => {
      try {
        await api.deleteBelief(belief.id);
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const beliefs = data?.beliefs ?? [];
  const counts = data?.counts;
  const enabled = data?.enabled ?? true;
  const grouped = useMemo(() => {
    const mood: Belief[] = [];
    const opinion: Belief[] = [];
    for (const b of beliefs) {
      if (b.kind === "mood") mood.push(b);
      else opinion.push(b);
    }
    return { mood, opinion };
  }, [beliefs]);

  if (!enabled) {
    return (
      <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3 text-[11px] text-ink-100/40">
        Belief tracking is disabled. Enable
        <code className="mx-1">belief_tracking_enabled</code>
        in agent settings to surface theory-of-mind beliefs here.
      </div>
    );
  }

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="What Aiko currently thinks you feel about specific topics (mood) or what you think about them (opinion). Mood beliefs flip to contradicted when the live affect read disagrees; opinion beliefs flip when your message lexically contradicts the prediction."
        >
          Beliefs
          {counts ? (
            <span className="ml-2 text-ink-100/40">
              ({counts.active} active · {counts.contradicted} contradicted)
            </span>
          ) : null}
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
        >
          {loading ? "..." : "refresh"}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>kind:</span>
        {(["all", "mood", "opinion"] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setKindFilter(k as BeliefKind | "all")}
            className={
              "rounded border px-1.5 py-0.5 " +
              (kindFilter === k
                ? "border-ink-400 bg-ink-400/10 text-ink-100"
                : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
            }
          >
            {k}
          </button>
        ))}
        <span className="ml-2">status:</span>
        {BELIEF_STATUS_FILTERS.map((opt) => (
          <button
            key={opt.id}
            type="button"
            onClick={() => setStatusFilter(opt.id)}
            className={
              "rounded border px-1.5 py-0.5 " +
              (statusFilter === opt.id
                ? "border-ink-400 bg-ink-400/10 text-ink-100"
                : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
            }
          >
            {opt.label}
          </button>
        ))}
      </div>
      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}
      {beliefs.length === 0 ? (
        <p className="text-[11px] text-ink-100/40">
          No beliefs in this view. Aiko's K2 worker mines fresh predictions
          from recent turns; she can also tag them inline.
        </p>
      ) : (
        <div className="space-y-3">
          {grouped.mood.length > 0 ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-ink-100/40">
                Mood ({grouped.mood.length})
              </div>
              <ul className="space-y-1">
                {grouped.mood.map((b) => (
                  <BeliefCard
                    key={b.id}
                    belief={b}
                    onContradict={handleContradict}
                    onConfirm={handleConfirm}
                    onDelete={handleDelete}
                  />
                ))}
              </ul>
            </div>
          ) : null}
          {grouped.opinion.length > 0 ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-ink-100/40">
                Opinion ({grouped.opinion.length})
              </div>
              <ul className="space-y-1">
                {grouped.opinion.map((b) => (
                  <BeliefCard
                    key={b.id}
                    belief={b}
                    onContradict={handleContradict}
                    onConfirm={handleConfirm}
                    onDelete={handleDelete}
                  />
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

interface BeliefCardProps {
  belief: Belief;
  onContradict: (b: Belief) => void | Promise<void>;
  onConfirm: (b: Belief) => void | Promise<void>;
  onDelete: (b: Belief) => void | Promise<void>;
}

function BeliefCard({
  belief,
  onContradict,
  onConfirm,
  onDelete,
}: BeliefCardProps) {
  const statusTone =
    belief.status === "contradicted"
      ? "border-rose-400/30 bg-rose-500/5"
      : belief.status === "confirmed"
      ? "border-emerald-400/30 bg-emerald-500/5"
      : belief.status === "stale"
      ? "border-white/10 bg-white/[0.02] opacity-70"
      : "border-amber-400/30 bg-amber-500/5";
  const gapPing =
    belief.gap_seen_at && belief.status === "contradicted"
      ? "ring-1 ring-rose-400/40"
      : "";
  return (
    <li
      className={`rounded border p-2 text-[11px] ${statusTone} ${gapPing}`}
    >
      <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/60">
        <span>{belief.kind}</span>
        <span>·</span>
        <span>{belief.status}</span>
        <span>·</span>
        <span>conf {belief.confidence.toFixed(2)}</span>
        <span>·</span>
        <span>source {belief.source}</span>
        <span>·</span>
        <span>{formatRelative(belief.observed_at)}</span>
      </div>
      <div className="text-ink-100/80">
        <span className="font-medium">{belief.topic}</span>
        <span className="text-ink-100/40"> — </span>
        <span>{belief.predicted_state}</span>
      </div>
      {belief.kind === "mood" && belief.valence !== null ? (
        <div className="mt-1 text-[10px] text-ink-100/50">
          predicted valence {belief.valence.toFixed(2)}
          {belief.arousal !== null
            ? ` · arousal ${belief.arousal.toFixed(2)}`
            : ""}
        </div>
      ) : null}
      {belief.gap_seen_at ? (
        <div className="mt-1 text-[10px] text-rose-200/80">
          gap seen {formatRelative(belief.gap_seen_at)}
        </div>
      ) : null}
      <div className="mt-2 flex flex-wrap items-center gap-1 text-[10px]">
        <button
          type="button"
          onClick={() => void onContradict(belief)}
          className="rounded border border-white/10 px-1.5 py-0.5 hover:border-rose-300 hover:text-rose-200"
        >
          mark contradicted
        </button>
        <button
          type="button"
          onClick={() => void onConfirm(belief)}
          className="rounded border border-white/10 px-1.5 py-0.5 hover:border-emerald-300 hover:text-emerald-200"
        >
          mark confirmed
        </button>
        <button
          type="button"
          onClick={() => void onDelete(belief)}
          className="rounded border border-white/10 px-1.5 py-0.5 hover:border-rose-400 hover:text-rose-200"
        >
          delete
        </button>
      </div>
    </li>
  );
}
