import { useCallback, useMemo } from "react";
import { api } from "../../../api";
import { useAssistantStore } from "../../../store";
import type { Belief, BeliefKind, BeliefStatus } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

// "Active" is the review queue, so it is labelled as one. A belief that
// gets re-observed with the same state and has never been contradicted
// now promotes itself to confirmed, so what is left in this bucket is
// genuinely the set no rule can settle: seen once and never again, or
// wrong at least once before.
const BELIEF_STATUS_FILTERS: {
  id: BeliefStatus | "all";
  label: string;
  hint: string;
}[] = [
  {
    id: "active",
    label: "Needs review",
    hint: "Believed, but not corroborated. Anything seen twice with the same reading promotes itself, so these are the ones no rule can settle.",
  },
  {
    id: "contradicted",
    label: "Contradicted",
    hint: "The live signal disagreed with this. Still on file — a contradicted row can come back if she re-observes it.",
  },
  {
    id: "confirmed",
    label: "Confirmed",
    hint: "Corroborated. These are the only beliefs she will speak from, and they are still gap-checked.",
  },
  { id: "stale", label: "Stale", hint: "Untouched long enough to age out." },
  { id: "all", label: "All", hint: "Every belief, any status." },
];

/** How many times this exact reading has been observed. */
function observationCount(belief: Belief): number {
  const raw = belief.metadata?.observations;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 1;
}

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

  const loader = useCallback(async () => {
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
  }, [kindFilter, statusFilter, setBeliefView]);
  const { loading, error, setError, refresh } = useAsyncResource<void>(
    loader,
    undefined,
  );

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
    [applyBeliefUpdated, setError],
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
    [applyBeliefUpdated, setError],
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
    [applyBeliefDeleted, setError],
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
          title="What Aiko currently thinks you feel about specific topics (mood) or what you think about them (opinion). Mood beliefs are checked against the live affect read on turns actually about that topic; opinion beliefs flip when your message lexically contradicts the prediction. Confirmed beliefs are the ones she speaks from — and they stay gap-checked, so confirming one is not the same as archiving it."
        >
          Beliefs
          {counts ? (
            <span className="ml-2 text-ink-100/40">
              ({counts.active} to review · {counts.confirmed} confirmed ·{" "}
              {counts.contradicted} contradicted)
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
            title={opt.hint}
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
  const seen = observationCount(belief);
  // gap_seen_at is permanent, so a row that was wrong once can never
  // auto-confirm however often it is re-observed. Those are the rows
  // that will sit in the queue indefinitely unless a person rules on
  // them, and nothing else on the card says so.
  const blockedFromAutoConfirm =
    belief.status === "active" && belief.gap_seen_at !== null;
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
        <span title="How many times this exact reading has been observed. Two is enough to confirm itself.">
          seen {seen}×
        </span>
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
      {blockedFromAutoConfirm ? (
        <div
          className="mt-1 text-[10px] text-amber-200/80"
          title="This belief was contradicted at some point, so it will never promote itself no matter how often she re-observes it. Only you can settle it."
        >
          was wrong before — won't self-confirm
        </div>
      ) : null}
      {belief.kind === "mood" && belief.valence === null ? (
        <div
          className="mt-1 text-[10px] text-ink-100/40"
          title="No numeric affect coordinates, so the gap detector cannot check this one against how you actually sound. Older mood beliefs were all written this way."
        >
          no affect reading — can't be auto-checked
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
