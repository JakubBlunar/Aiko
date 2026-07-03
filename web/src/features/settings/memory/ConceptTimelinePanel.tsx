import { useCallback, useMemo, useState } from "react";
import { api } from "../../../api";
import type { ConceptEvent, ConceptTimeline } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

const SUBJECT_ALL = "all";

// Aiko's "aha!" moments: an append-only, day-grouped feed of when she
// first formed each higher-order concept. Read-only -- deleting a concept
// never erases its discovery, so this stands as a permanent history.
export function ConceptTimelinePanel() {
  const loader = useCallback(() => api.getConceptTimeline({ limit: 500 }), []);
  const { data, loading, error, refresh } =
    useAsyncResource<ConceptTimeline | null>(loader, null);

  const [subjectFilter, setSubjectFilter] = useState<string>(SUBJECT_ALL);

  const enabled = data?.enabled ?? true;
  const events = useMemo(() => data?.events ?? [], [data]);

  const subjectOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const e of events) seen.add(e.subject);
    return [SUBJECT_ALL, ...Array.from(seen).sort()];
  }, [events]);

  const visible = useMemo(
    () =>
      events.filter(
        (e) => subjectFilter === SUBJECT_ALL || e.subject === subjectFilter,
      ),
    [events, subjectFilter],
  );

  // Group by calendar day (already newest-first from the backend).
  const groups = useMemo(() => {
    const out: { day: string; events: ConceptEvent[] }[] = [];
    for (const e of visible) {
      const day = dayLabel(e.created_at);
      const last = out[out.length - 1];
      if (last && last.day === day) last.events.push(e);
      else out.push({ day, events: [e] });
    }
    return out;
  }, [visible]);

  if (data && !enabled) {
    return (
      <Panel className="text-[11px] text-ink-100/40">
        The concept layer is disabled. Enable
        <code className="mx-1">concepts_enabled</code>
        in agent settings and run synthesis so Aiko starts recording her
        discovery timeline here.
      </Panel>
    );
  }

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="An append-only timeline of Aiko's concept discoveries -- the moments she first abstracted a higher-order concept about herself or the user. Deleting a concept does not remove its discovery event."
        >
          Discoveries
          {data ? (
            <span className="ml-2 text-ink-100/40">({data.total})</span>
          ) : null}
        </span>
        <RefreshButton onClick={() => void refresh()} loading={loading} />
      </div>

      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>subject:</span>
        {subjectOptions.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSubjectFilter(s)}
            className={
              "rounded border px-1.5 py-0.5 " +
              (subjectFilter === s
                ? "border-ink-400 bg-ink-400/10 text-ink-100"
                : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
            }
          >
            {s}
          </button>
        ))}
      </div>

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {visible.length === 0 ? (
        <EmptyState>
          No discoveries yet. As Aiko's L2 worker synthesises concepts from
          her topic clusters and self-memories, each first sighting lands
          here as an "aha!" moment.
        </EmptyState>
      ) : (
        <div className="space-y-3">
          {groups.map((g) => (
            <div key={g.day}>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-ink-100/40">
                {g.day}
              </div>
              <ul className="space-y-1 border-l border-white/10 pl-3">
                {g.events.map((e) => (
                  <TimelineCard key={e.id} event={e} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

const EVENT_TONE: Record<string, string> = {
  discovered: "border-sky-400/30 bg-sky-500/5",
  reinforced: "border-emerald-400/30 bg-emerald-500/5",
  promoted: "border-violet-400/30 bg-violet-500/5",
  contradicted: "border-rose-400/30 bg-rose-500/5",
  retired: "border-white/10 bg-white/[0.02] opacity-70",
};

function TimelineCard({ event }: { event: ConceptEvent }) {
  const tone = EVENT_TONE[event.event_type] ?? "border-white/10 bg-white/[0.02]";
  const noveltyPct = Math.round(Math.max(0, Math.min(1, event.novelty)) * 100);
  return (
    <li className={`rounded border p-2 text-[11px] ${tone}`}>
      <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/60">
        <span className="font-medium text-ink-100/80">{event.event_type}</span>
        <span>·</span>
        <span>{event.subject}</span>
        <span>·</span>
        <span>{event.kind}</span>
        <span>·</span>
        <span>conf {event.confidence.toFixed(2)}</span>
        <span>·</span>
        <span title="Novelty: 1 - cosine similarity to the nearest prior concept of the same subject/kind at synthesis time.">
          novelty {event.novelty.toFixed(2)}
        </span>
      </div>

      {/* Novelty bar. */}
      <div className="mb-1 h-1 w-full overflow-hidden rounded bg-white/10">
        <div
          className="h-full bg-sky-300/60"
          style={{ width: `${noveltyPct}%` }}
        />
      </div>

      {/* Full label, wrapped -- never truncated. */}
      <div className="whitespace-pre-wrap break-words font-medium text-ink-100/85">
        {event.label}
      </div>

      {event.reason ? (
        <div className="mt-1 whitespace-pre-wrap break-words text-ink-100/60">
          <span className="text-ink-100/40">reason: </span>
          {event.reason}
        </div>
      ) : null}

      <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-ink-100/40">
        <span>{absoluteTime(event.created_at)}</span>
        <span>·</span>
        <span>{formatRelative(event.created_at)}</span>
        {event.source_kinds ? (
          <>
            <span>·</span>
            <span>{event.source_kinds}</span>
          </>
        ) : null}
        {event.concept_id == null ? (
          <>
            <span>·</span>
            <span className="italic">concept deleted</span>
          </>
        ) : null}
      </div>
    </li>
  );
}

function dayLabel(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "unknown date";
  return new Date(t).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function absoluteTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  return new Date(t).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}
