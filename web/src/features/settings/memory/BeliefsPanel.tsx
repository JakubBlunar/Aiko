import { useCallback, useMemo, useState } from "react";
import { api } from "../../../api";
import { useAssistantStore } from "../../../store";
import type { Belief, BeliefKind, BeliefStatus } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

const BELIEF_STATUS_FILTERS: {
  id: BeliefStatus | "all";
  label: string;
  hint: string;
}[] = [
  {
    id: "active",
    label: "Tentative",
    hint: "Possible interpretations. You do not need to review every thought.",
  },
  {
    id: "contradicted",
    label: "Contradicted",
    hint: "A reading marked as incorrect. Earlier automatic judgements remain historical records.",
  },
  {
    id: "confirmed",
    label: "Confirmed",
    hint: "Marked as correct. Temporary readings still expire when their situation ends.",
  },
  { id: "stale", label: "Stale", hint: "Untouched long enough to age out." },
  { id: "all", label: "All", hint: "Every belief, any status." },
];

export function BeliefsPanel() {
  const [includeLegacy, setIncludeLegacy] = useState(false);
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
      include_legacy: includeLegacy,
      limit: 100,
      kind: kindFilter === "all" ? undefined : kindFilter,
      status: statusFilter === "all" ? undefined : statusFilter,
    });
    setBeliefView({
      items: snapshot.beliefs,
      counts: snapshot.counts ?? null,
      enabled: snapshot.enabled,
    });
  }, [kindFilter, statusFilter, setBeliefView, includeLegacy]);
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

  const handleCorrect = useCallback(
    async (belief: Belief, interpretation: string, situation: string, uncertainty: string) => {
      const res = await api.updateBelief(belief.id, {
        predicted_state: interpretation,
        ...(situation.trim() ? { situation } : {}),
        ...(uncertainty.trim() ? { uncertainty } : {}),
      });
      applyBeliefUpdated(res.belief);
    }, [applyBeliefUpdated],
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

  const beliefs = beliefView.items.filter((b) => includeLegacy || b.metadata.interpretation_version === 2);
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
          title="Aiko's tentative interpretations of an exchange: what it might mean, why she thinks so, and what remains uncertain. New ideas can go beyond established concepts. Repetition is not confirmation."
        >
          Beliefs
          {counts ? (
            <span className="ml-2 text-ink-100/40">
              ({counts.active} tentative · {counts.confirmed} confirmed ·{" "}
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
      <label className="flex items-center gap-2 text-[11px] text-ink-100/50">
        <input type="checkbox" checked={includeLegacy} onChange={(e) => setIncludeLegacy(e.target.checked)} />
        Include earlier readings with unverified scope
      </label>
      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}
      {beliefs.length === 0 ? (
        <EmptyState>
          No readings in this view. Aiko can form a new interpretation when a recent exchange suggests something useful.
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
                    onCorrect={handleCorrect}
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
                    onCorrect={handleCorrect}
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
  onCorrect: (b: Belief, interpretation: string, situation: string, uncertainty: string) => Promise<void>;
}

export function BeliefCard({
  belief, onContradict, onConfirm, onDelete, onCorrect,
}: BeliefCardProps) {
  const [editing, setEditing] = useState(false);
  const [reading, setReading] = useState(belief.predicted_state);
  const text = (key: string) => typeof belief.metadata[key] === "string"
    ? belief.metadata[key] as string : "";
  const [situation, setSituation] = useState(text("situation"));
  const [uncertainty, setUncertainty] = useState(text("uncertainty"));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const structured = belief.metadata.interpretation_version === 2;
  const userConfirmed = belief.status === "confirmed" && belief.metadata.confirmation_basis === "user";
  const evidence = Array.isArray(belief.metadata.evidence)
    ? belief.metadata.evidence.filter((e): e is Record<string, unknown> =>
      typeof e === "object" && e !== null && typeof e.quote === "string") : [];
  const expired = !!text("expires_at") && Date.parse(text("expires_at")) <= Date.now();
  const label = expired || belief.status === "stale" ? "Out of date"
    : userConfirmed ? "You confirmed" : belief.status === "contradicted" ? "Marked incorrect"
    : structured ? "Tentative interpretation" : "Earlier reading · scope unverified";
  const save = async () => {
    setSaving(true);
    setError("");
    try {
      await onCorrect(belief, reading, situation, uncertainty);
      setEditing(false);
    } catch (err) { setError(String(err)); }
    finally { setSaving(false); }
  };
  return (
    <li className="rounded border border-white/10 p-3 text-[11px] space-y-2">
      <div className="flex flex-wrap justify-between gap-1 text-[10px] text-ink-100/50">
        <span>{label}</span><span>{formatRelative(belief.observed_at)}</span>
      </div>
      <div className="font-medium text-ink-100/70">{belief.topic}</div>
      <p className="whitespace-pre-wrap text-ink-100/90">{belief.predicted_state}</p>
      {structured && <dl className="space-y-1 text-ink-100/65">
        {([ ["reason", "Why"], ["situation", "Applies to"],
          ["uncertainty", "Still uncertain"], ["revise_when", "Reconsider if"] ] as const)
          .map(([key, title]) => text(key) ? <div key={key}>
            <dt className="inline font-medium">{title}: </dt><dd className="inline">{text(key)}</dd>
          </div> : null)}
        <div><dt className="inline font-medium">Applies until: </dt>
          <dd className="inline">{text("expires_at") ? new Date(text("expires_at")).toLocaleString() : "Unknown"}</dd>
        </div>
      </dl>}
      {evidence.length > 0 && <details className="text-ink-100/60">
        <summary className="cursor-pointer">Supporting words ({evidence.length})</summary>
        <p className="mt-1 text-ink-100/40">These support the interpretation; they do not prove it.</p>
        {evidence.map((e, i) => <blockquote key={i} className="mt-2 border-l border-white/20 pl-2">
          <p className="whitespace-pre-wrap">{String(e.quote)}</p>
          <p className="text-[10px] text-ink-100/40">Message {String(e.message_id)} · {String(e.created_at ?? "")}</p>
        </blockquote>)}
      </details>}
      <details className="text-[10px] text-ink-100/40">
        <summary className="cursor-pointer">Details</summary>
        <p>Source: {belief.source} · Model confidence: {belief.confidence.toFixed(2)}</p>
        <p>Extractions are not independent confirmations. Earlier status labels may come from automatic checks.</p>
      </details>
      {editing ? <div className="space-y-2">
        <label className="block">Interpretation
          <textarea aria-label="Interpretation" value={reading} maxLength={600}
            onChange={(e) => setReading(e.target.value)}
            className="mt-1 block w-full rounded border border-white/20 bg-black/20 p-2" />
        </label>
        <label className="block">Situation it applies to
          <textarea aria-label="Situation it applies to" value={situation} maxLength={300}
            onChange={(e) => setSituation(e.target.value)}
            className="mt-1 block w-full rounded border border-white/20 bg-black/20 p-2" />
        </label>
        <label className="block">What remains uncertain
          <textarea aria-label="What remains uncertain" value={uncertainty} maxLength={300}
            onChange={(e) => setUncertainty(e.target.value)}
            className="mt-1 block w-full rounded border border-white/20 bg-black/20 p-2" />
        </label>
        {error && <ErrorBanner compact>{error}</ErrorBanner>}
        <button type="button" disabled={saving || !reading.trim()} onClick={() => void save()}
          className="rounded border border-white/20 px-2 py-1 disabled:opacity-40">Save correction</button>
        <button type="button" disabled={saving} onClick={() => setEditing(false)}
          className="ml-2 px-2 py-1">Cancel</button>
      </div> : <div className="flex flex-wrap gap-2 text-[10px]">
        <button type="button" onClick={() => void onConfirm(belief)}
          className="rounded border border-white/10 px-2 py-1">That fits</button>
        <button type="button" onClick={() => void onContradict(belief)}
          className="rounded border border-white/10 px-2 py-1">Not quite</button>
        <button type="button" onClick={() => {
          setReading(belief.predicted_state); setSituation(text("situation"));
          setUncertainty(text("uncertainty")); setEditing(true);
        }} className="rounded border border-white/10 px-2 py-1">Correct wording or scope</button>
        <button type="button" onClick={() => void onDelete(belief)}
          className="rounded border border-white/10 px-2 py-1">Delete</button>
      </div>}
    </li>
  );
}
