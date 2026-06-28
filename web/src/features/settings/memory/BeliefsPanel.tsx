import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../../api";
import { useAssistantStore } from "../../../store";
import type { Belief, BeliefKind, BeliefStatus } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

const BELIEF_STATUS_FILTERS: { id: BeliefStatus | "all"; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "contradicted", label: "Contradicted" },
  { id: "confirmed", label: "Confirmed" },
  { id: "stale", label: "Stale" },
  { id: "all", label: "All" },
];

export function BeliefsPanel() {
  // Items + filters live in the global store so the panel stays live as
  // the K2 worker / ``[[predict:...]]`` tags / the gap detector flip
  // beliefs over WebSocket (see ``applyBelief*`` reducers). Only the
  // transient request status stays local.
  const beliefView = useAssistantStore((s) => s.beliefView);
  const setBeliefView = useAssistantStore((s) => s.setBeliefView);
  const kindFilter = beliefView.kindFilter;
  const statusFilter = beliefView.statusFilter;
  const setKindFilter = useAssistantStore((s) => s.setBeliefKindFilter);
  const setStatusFilter = useAssistantStore((s) => s.setBeliefStatusFilter);
  const applyBeliefUpdated = useAssistantStore((s) => s.applyBeliefUpdated);
  const applyBeliefDeleted = useAssistantStore((s) => s.applyBeliefDeleted);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snapshot = await api.listBeliefs({
        limit: 100,
        kind: kindFilter === "all" ? undefined : kindFilter,
        status: statusFilter === "all" ? undefined : statusFilter,
      });
      setBeliefView({
        items: snapshot.beliefs,
        counts: snapshot.counts ?? null,
        enabled: snapshot.enabled,
      });
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [kindFilter, statusFilter, setBeliefView]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleContradict = useCallback(
    async (belief: Belief) => {
      try {
        const res = await api.updateBelief(belief.id, {
          status: "contradicted",
        });
        applyBeliefUpdated(res.belief);
      } catch (err) {
        setError(String(err));
      }
    },
    [applyBeliefUpdated],
  );

  const handleConfirm = useCallback(
    async (belief: Belief) => {
      try {
        const res = await api.updateBelief(belief.id, { status: "confirmed" });
        applyBeliefUpdated(res.belief);
      } catch (err) {
        setError(String(err));
      }
    },
    [applyBeliefUpdated],
  );

  const handleDelete = useCallback(
    async (belief: Belief) => {
      try {
        await api.deleteBelief(belief.id);
        applyBeliefDeleted(belief.id);
      } catch (err) {
        setError(String(err));
      }
    },
    [applyBeliefDeleted],
  );

  const beliefs = beliefView.items;
  const counts = beliefView.counts ?? undefined;
  const enabled = beliefView.enabled;
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
      <Panel className="text-[11px] text-ink-100/40">
        Belief tracking is disabled. Enable
        <code className="mx-1">belief_tracking_enabled</code>
        in agent settings to surface theory-of-mind beliefs here.
      </Panel>
    );
  }

  return (
    <Panel>
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
        <RefreshButton onClick={refresh} loading={loading} />
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
      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}
      {beliefs.length === 0 ? (
        <EmptyState>
          No beliefs in this view. Aiko's K2 worker mines fresh predictions
          from recent turns; she can also tag them inline.
        </EmptyState>
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
    </Panel>
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
