import { useCallback, useMemo, useState } from "react";
import { api } from "../../../api";
import type {
  ConceptLearningEvent,
  ConceptLearningFeed,
  ConceptProvenance,
  EvolutionDiaryEntry,
  EvolutionDiaryFeed,
} from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

const ALL = "all";

// L17e: the history-of-thought browser. Where Discoveries is the raw
// lifecycle log, this is the causal record -- only the movement the L17b
// classifier judged to be real learning, each entry saying what the
// belief was, what it became, and why. Clicking one opens the full
// provenance drill-down for that belief.
export function ConceptEvolutionPanel() {
  const loader = useCallback(() => api.getConceptLearning({ limit: 200 }), []);
  const { data, loading, error, refresh } =
    useAsyncResource<ConceptLearningFeed | null>(loader, null);

  const [shapeFilter, setShapeFilter] = useState<string>(ALL);
  const [subjectFilter, setSubjectFilter] = useState<string>(ALL);
  const [openId, setOpenId] = useState<number | null>(null);
  const [running, setRunning] = useState(false);

  const enabled = data?.enabled ?? true;
  const events = useMemo(() => data?.events ?? [], [data]);

  const shapeOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const e of events) seen.add(e.shape);
    return [ALL, ...Array.from(seen).sort()];
  }, [events]);

  const subjectOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const e of events) seen.add(e.subject);
    return [ALL, ...Array.from(seen).sort()];
  }, [events]);

  const visible = useMemo(
    () =>
      events.filter(
        (e) =>
          (shapeFilter === ALL || e.shape === shapeFilter) &&
          (subjectFilter === ALL || e.subject === subjectFilter),
      ),
    [events, shapeFilter, subjectFilter],
  );

  const runDrift = useCallback(async () => {
    setRunning(true);
    try {
      await api.runConceptDrift();
      await refresh();
    } finally {
      setRunning(false);
    }
  }, [refresh]);

  if (data && !enabled) {
    return (
      <Panel className="text-[11px] text-ink-100/40">
        Concept evolution is disabled. Enable
        <code className="mx-1">concepts_enabled</code>
        and
        <code className="mx-1">concept_drift_enabled</code>
        so Aiko starts recording how her understanding changes.
      </Panel>
    );
  }

  return (
    <>
    <EvolutionDiaryPanel />
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="How Aiko's beliefs have changed and why. Unlike Discoveries -- which logs every lifecycle move -- this records only changes judged to be real learning, and it is never pruned."
        >
          Evolution
          {data ? (
            <span className="ml-2 text-ink-100/40">({data.total})</span>
          ) : null}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void runDrift()}
            disabled={running}
            className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-100/60 hover:border-ink-400/60 disabled:opacity-40"
            title="Run one drift pass now: apply any staged relabels and record what changed."
          >
            {running ? "running…" : "run drift"}
          </button>
          <RefreshButton onClick={() => void refresh()} loading={loading} />
        </div>
      </div>

      {data && Object.keys(data.counts ?? {}).length > 0 ? (
        <div className="flex flex-wrap gap-2 text-[10px] text-ink-100/40">
          {Object.entries(data.counts).map(([shape, count]) => (
            <span key={shape}>
              {shape} <span className="text-ink-100/70">{count}</span>
            </span>
          ))}
        </div>
      ) : null}

      <FilterRow
        label="shape"
        options={shapeOptions}
        value={shapeFilter}
        onChange={setShapeFilter}
      />
      <FilterRow
        label="subject"
        options={subjectOptions}
        value={subjectFilter}
        onChange={setSubjectFilter}
      />

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {visible.length === 0 ? (
        <EmptyState>
          Nothing yet. Beliefs have to move -- sharpen into a better
          wording, be superseded by a more precise one, or lose their
          support -- before there is anything to record here.
        </EmptyState>
      ) : (
        <ul className="space-y-1">
          {visible.map((e) => (
            <LearningCard
              key={e.id}
              event={e}
              open={openId === e.id}
              onToggle={() => setOpenId(openId === e.id ? null : e.id)}
            />
          ))}
        </ul>
      )}
    </Panel>
    </>
  );
}

// L17f: the diary sitting above the raw feed. Where a learning event is
// one change, an entry is Aiko's own account of a whole period -- and it
// is the fastest read on whether the pipeline below it is healthy. Each
// entry's cited concepts reuse the same provenance drill-down, so a line
// that sounds invented can be checked against its evidence immediately.
function EvolutionDiaryPanel() {
  const loader = useCallback(() => api.getEvolutionDiary({ limit: 50 }), []);
  const { data, loading, error, refresh } =
    useAsyncResource<EvolutionDiaryFeed | null>(loader, null);
  const [running, setRunning] = useState(false);

  const compose = useCallback(async () => {
    setRunning(true);
    try {
      await api.runEvolutionDiary();
      await refresh();
    } finally {
      setRunning(false);
    }
  }, [refresh]);

  if (data && !data.enabled) return null;
  const entries = data?.entries ?? [];

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Aiko's own periodic account of how her understanding has changed, composed only from the reasons recorded below. Gaps are meaningful: a period with nothing above the salience floor writes no entry rather than padding."
        >
          Diary
          {data ? (
            <span className="ml-2 text-ink-100/40">({data.total})</span>
          ) : null}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void compose()}
            disabled={running}
            className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-100/60 hover:border-ink-400/60 disabled:opacity-40"
            title="Compose one entry now, skipping the cooldown. The salient-change floor still applies, so an empty period stays empty."
          >
            {running ? "writing…" : "write entry"}
          </button>
          <RefreshButton onClick={() => void refresh()} loading={loading} />
        </div>
      </div>

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {entries.length === 0 ? (
        <EmptyState>
          No entries yet. Enough beliefs have to move within one period
          before there is a change worth writing about -- until then the
          changes below are held, not lost.
        </EmptyState>
      ) : (
        <ul className="space-y-1">
          {entries.map((e) => (
            <DiaryCard key={e.id} entry={e} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function DiaryCard({ entry }: { entry: EvolutionDiaryEntry }) {
  const [openId, setOpenId] = useState<number | null>(null);
  const shapes = Object.entries(entry.shape_counts ?? {});
  return (
    <li className="rounded border border-indigo-400/25 bg-indigo-500/5 p-2 text-[11px]">
      <div className="whitespace-pre-wrap break-words text-ink-100/85">
        {entry.entry}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-ink-100/40">
        <span>{formatRelative(entry.created_at)}</span>
        {shapes.length > 0 ? (
          <>
            <span>·</span>
            <span>
              {shapes.map(([shape, n]) => `${n} ${shape}`).join(", ")}
            </span>
          </>
        ) : null}
      </div>

      {entry.concept_ids.length > 0 ? (
        <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-ink-100/40">
          <span title="The beliefs this entry was composed from. Open one to check the paragraph against its evidence.">
            from:
          </span>
          {entry.concept_ids.map((cid) => (
            <button
              key={cid}
              type="button"
              onClick={() => setOpenId(openId === cid ? null : cid)}
              className={
                "rounded border px-1 py-0.5 " +
                (openId === cid
                  ? "border-ink-400 bg-ink-400/10 text-ink-100"
                  : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
              }
            >
              #{cid}
            </button>
          ))}
        </div>
      ) : null}

      {openId != null ? <ProvenanceDetail conceptId={openId} /> : null}
    </li>
  );
}

function FilterRow({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: string[];
  value: string;
  onChange: (next: string) => void;
}) {
  if (options.length <= 1) return null;
  return (
    <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
      <span>{label}:</span>
      {options.map((o) => (
        <button
          key={o}
          type="button"
          onClick={() => onChange(o)}
          className={
            "rounded border px-1.5 py-0.5 " +
            (value === o
              ? "border-ink-400 bg-ink-400/10 text-ink-100"
              : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
          }
        >
          {o}
        </button>
      ))}
    </div>
  );
}

const SHAPE_TONE: Record<string, string> = {
  succession: "border-violet-400/30 bg-violet-500/5",
  relabel: "border-sky-400/30 bg-sky-500/5",
  emergence: "border-emerald-400/30 bg-emerald-500/5",
  revival: "border-amber-400/30 bg-amber-500/5",
  loss: "border-white/10 bg-white/[0.02] opacity-80",
};

const SHAPE_HINT: Record<string, string> = {
  succession:
    "A belief faded while a near-identical, more precise one rose on overlapping evidence. Two rows, because a proposal at or above the dedupe cosine folds into the existing concept instead of forming a new one.",
  relabel:
    "The same belief, rewritten in place because a later proposal said it better. Every wording it has held is kept.",
  emergence: "Enough separate moments pointed the same way to call it a belief.",
  loss: "The support for this belief fell away and nothing replaced it.",
  revival: "A belief that had faded came back.",
};

function LearningCard({
  event,
  open,
  onToggle,
}: {
  event: ConceptLearningEvent;
  open: boolean;
  onToggle: () => void;
}) {
  const tone = SHAPE_TONE[event.shape] ?? "border-white/10 bg-white/[0.02]";
  return (
    <li className={`rounded border p-2 text-[11px] ${tone}`}>
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left"
        aria-expanded={open}
      >
        <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/60">
          <span
            className="font-medium text-ink-100/80"
            title={SHAPE_HINT[event.shape] ?? ""}
          >
            {event.shape}
          </span>
          <span>·</span>
          <span>{event.subject}</span>
          <span>·</span>
          <span>{event.kind}</span>
          <span>·</span>
          <span title="How much this change mattered: the shape's base score weighted by the belief's plasticity band, so equal movement counts for more in a sticky value than a fluid taste.">
            salience {event.salience.toFixed(2)}
          </span>
        </div>

        {event.old_label ? (
          <div className="whitespace-pre-wrap break-words text-ink-100/50 line-through decoration-ink-100/30">
            {event.old_label}
          </div>
        ) : null}
        <div className="whitespace-pre-wrap break-words font-medium text-ink-100/85">
          {event.new_label}
        </div>

        {event.because ? (
          <div className="mt-1 whitespace-pre-wrap break-words text-ink-100/60">
            <span className="text-ink-100/40">because: </span>
            {event.because}
          </div>
        ) : null}

        <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-ink-100/40">
          <span>{formatRelative(event.created_at)}</span>
          {event.prior_concept_id != null ? (
            <>
              <span>·</span>
              <span>from #{event.prior_concept_id}</span>
            </>
          ) : null}
          {event.concept_id != null ? (
            <>
              <span>·</span>
              <span>#{event.concept_id}</span>
            </>
          ) : null}
          {event.cosine != null ? (
            <>
              <span>·</span>
              <span>cos {event.cosine.toFixed(2)}</span>
            </>
          ) : null}
        </div>
      </button>

      {event.evidence_labels.length > 0 ? (
        <ul className="mt-1 space-y-0.5 border-l border-white/10 pl-2 text-[10px] text-ink-100/50">
          {event.evidence_labels.map((label, i) => (
            <li key={i} className="whitespace-pre-wrap break-words">
              {label}
            </li>
          ))}
        </ul>
      ) : null}

      {open && event.concept_id != null ? (
        <ProvenanceDetail conceptId={event.concept_id} />
      ) : null}
    </li>
  );
}

// The drill-down: one belief's whole arc. Loaded on expand so the feed
// stays a single request.
function ProvenanceDetail({ conceptId }: { conceptId: number }) {
  const loader = useCallback(
    () => api.getConceptProvenance(conceptId),
    [conceptId],
  );
  const { data, loading, error } = useAsyncResource<ConceptProvenance | null>(
    loader,
    null,
  );

  if (loading) {
    return <div className="mt-2 text-[10px] text-ink-100/40">loading…</div>;
  }
  if (error) return <ErrorBanner compact>{error}</ErrorBanner>;
  if (!data || !data.enabled) return null;

  const merged =
    data.resolved_id != null && data.resolved_id !== data.concept_id;

  return (
    <div className="mt-2 space-y-2 border-t border-white/10 pt-2 text-[10px]">
      <div className="text-ink-100/40">
        {data.exists ? (
          <>
            currently: <span className="text-ink-100/70">{data.label}</span>
            {data.status ? ` (${data.status})` : null}
          </>
        ) : (
          <span className="italic">this concept no longer exists</span>
        )}
        {merged ? (
          <span
            className="ml-1"
            title="This belief was merged into another. Its history is still reachable through the alias chain."
          >
            · merged into #{data.resolved_id}
          </span>
        ) : null}
      </div>

      {data.prior_labels && data.prior_labels.length > 1 ? (
        <div>
          <div className="mb-0.5 uppercase tracking-wide text-ink-100/40">
            wordings it has worn
          </div>
          <ul className="space-y-0.5 border-l border-white/10 pl-2 text-ink-100/60">
            {data.prior_labels.map((label, i) => (
              <li key={i} className="whitespace-pre-wrap break-words">
                {label}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {data.absorbed && data.absorbed.length > 0 ? (
        <div>
          <div className="mb-0.5 uppercase tracking-wide text-ink-100/40">
            absorbed
          </div>
          <ul className="space-y-0.5 border-l border-white/10 pl-2 text-ink-100/60">
            {data.absorbed.map((a) => (
              <li key={a.absorbed_id} className="break-words">
                #{a.absorbed_id} {a.absorbed_label}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {data.lifecycle && data.lifecycle.length > 0 ? (
        <div>
          <div className="mb-0.5 uppercase tracking-wide text-ink-100/40">
            lifecycle
          </div>
          <ul className="space-y-0.5 border-l border-white/10 pl-2 text-ink-100/60">
            {data.lifecycle.map((p) => (
              <li key={p.id} className="break-words">
                <span className="text-ink-100/80">{p.event_type}</span>
                <span className="ml-1 text-ink-100/40">
                  {formatRelative(p.created_at)}
                </span>
                {p.reason ? (
                  <span className="ml-1 text-ink-100/50">{p.reason}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
