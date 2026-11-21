import { useCallback, useState } from "react";
import { api } from "../../../api";
import type {
  RecordedChoiceHistory,
  SelfHistoryArc,
  SelfHistoryEntry,
  SelfHistoryEra,
} from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

export function SelfHistoryPanel() {
  const [subject, setSubject] = useState<string>("aiko");
  const loader = useCallback(
    () => api.getSelfHistory({ subject, eras: 12 }),
    [subject],
  );
  const { data, loading, error, refresh } =
    useAsyncResource<SelfHistoryArc | null>(loader, null);

  if (data && !data.enabled) {
    return (
      <Panel className="text-[11px] text-ink-100/40">
        Self-history needs the concept layer. Enable
        <code className="mx-1">concepts_enabled</code>
        so Aiko starts building a record she can look back on.
      </Panel>
    );
  }

  const eras = data?.eras ?? [];

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Aiko’s record of changing beliefs and recent activity choices."
        >
          Story
          {data ? (
            <span className="ml-2 text-ink-100/40">
              {data.total_concepts} beliefs · {Math.round(data.span_days)}d
            </span>
          ) : null}
        </span>
        <div className="flex items-center gap-2">
          {(["aiko", "user"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setSubject(s)}
              className={
                "rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wide " +
                (subject === s
                  ? "border-ink-400 bg-ink-400/10 text-ink-100"
                  : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
              }
              title={
                s === "aiko"
                  ? "Her own history: what she has believed about herself."
                  : "How her read on you has evolved."
              }
            >
              {s === "aiko" ? "hers" : "yours"}
            </button>
          ))}
          <RefreshButton onClick={() => void refresh()} loading={loading} />
        </div>
      </div>

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {data?.thin_record ? (
        <div
          className="rounded border border-amber-400/30 bg-amber-500/5 p-2 text-[10px] text-ink-100/70"
        >
          No record of substantial changes in beliefs yet.
        </div>
      ) : null}

      {subject === "aiko" && data?.subject === "aiko" && data.choice_history ? (
        <ChoiceHistory history={data.choice_history} />
      ) : null}

      {data && Object.keys(data.counts ?? {}).length > 0 ? (
        <div className="flex flex-wrap gap-2 text-[10px] text-ink-100/40">
          {Object.entries(data.counts).map(([change, count]) => (
            <span key={change} title={CHANGE_HINT[change] ?? ""}>
              {change} <span className="text-ink-100/70">{count}</span>
            </span>
          ))}
        </div>
      ) : null}

      {eras.length === 0 ? (
        <EmptyState>
          Nothing to look back on yet. Beliefs have to form, and then move,
          before there is an arc to walk.
        </EmptyState>
      ) : (
        <ol className="space-y-2">
          {eras.map((era) => (
            <EraBlock key={era.start} era={era} />
          ))}
        </ol>
      )}
    </Panel>
  );
}

function ChoiceHistory({ history }: { history: RecordedChoiceHistory }) {
  const { finding, coverage } = history;
  const evidenceGroups = [
    { label: "Earlier", sources: finding?.earlier ?? [] },
    { label: finding?.earlier ? "Later" : "Recorded occasions", sources: finding?.later ?? [] },
  ].filter(({ sources }) => sources.length > 0);
  const formatDay = (at: string) => new Date(at).toLocaleDateString();

  return (
    <section className="space-y-1 rounded border border-white/10 p-2 text-[11px] text-ink-100/80">
      <h3 className="font-medium">Recorded choices</h3>
      <p>{finding?.statement ?? "The retained history has no supported sketching finding yet."}</p>
      <p className="text-[10px] text-ink-100/50">
        {formatDay(history.from)} – {formatDay(history.to)} · {coverage.reviewed_runs} runs reviewed
        {coverage.missing_cue_receipts > 0 &&
          ` · ${coverage.missing_cue_receipts} choices lacked cue records`}
        {history.truncated && " · Review limited to the latest runs"}
      </p>
      {evidenceGroups.length > 0 && (
        <details className="text-[10px] text-ink-100/60">
          <summary className="cursor-pointer">Supporting trace events</summary>
          {evidenceGroups.map(({ label, sources }) => (
            <div key={label} className="mt-1">
              <p>{label}</p>
              <ul className="space-y-0.5 pl-2">
                {sources.map((source) => (
                  <li key={source.completion_event_id} className="break-all" title={source.run_id}>
                    {formatDay(source.recorded_at)} · {source.choice_event_id !== null &&
                      `choice #${source.choice_event_id} · `}
                    completion #{source.completion_event_id}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </details>
      )}
    </section>
  );
}

const CHANGE_HINT: Record<string, string> = {
  flipped:
    "Replaced by a better reading, or rewritten in place. The most informative thing that can happen to a belief.",
  faded: "The support fell away and nothing replaced it.",
  revived: "It had faded, and then came back.",
  born: "Enough separate moments pointed the same way to call it a belief.",
  settled:
    "No recorded change: she has held this all along. Having beliefs is not the same as having changed, so these do not count towards the record being substantive.",
};

const CHANGE_TONE: Record<string, string> = {
  flipped: "border-violet-400/30 bg-violet-500/5",
  faded: "border-white/10 bg-white/[0.02] opacity-80",
  revived: "border-amber-400/30 bg-amber-500/5",
  born: "border-emerald-400/30 bg-emerald-500/5",
  settled: "border-white/10 bg-white/[0.02]",
};

function EraBlock({ era }: { era: SelfHistoryEra }) {
  return (
    <li>
      <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/50">
        <span className="font-medium text-ink-100/70">{era.label}</span>
        <span className="text-ink-100/30">{formatRelative(era.start)}</span>
        {era.truncated ? (
          <span
            className="text-ink-100/30"
            title="More happened in this period than is shown. The most informative changes are kept."
          >
            +{era.truncated} more
          </span>
        ) : null}
      </div>
      <ul className="space-y-1">
        {era.entries.map((entry) => (
          <EntryCard key={entry.concept_id} entry={entry} />
        ))}
      </ul>
    </li>
  );
}

function EntryCard({ entry }: { entry: SelfHistoryEntry }) {
  const tone = CHANGE_TONE[entry.change] ?? "border-white/10 bg-white/[0.02]";
  return (
    <li className={`rounded border p-2 text-[11px] ${tone}`}>
      <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/60">
        <span
          className="font-medium text-ink-100/80"
          title={CHANGE_HINT[entry.change] ?? ""}
        >
          {entry.change}
        </span>
        <span>·</span>
        <span>{entry.kind}</span>
        <span>·</span>
        <span>{entry.status}</span>
        <span>·</span>
        <span>#{entry.concept_id}</span>
      </div>

      {entry.prior_label ? (
        <div className="whitespace-pre-wrap break-words text-ink-100/50 line-through decoration-ink-100/30">
          {entry.prior_label}
        </div>
      ) : null}
      <div className="whitespace-pre-wrap break-words text-ink-100/85">
        {entry.label}
      </div>

      {entry.because ? (
        <div className="mt-1 whitespace-pre-wrap break-words text-ink-100/60">
          <span className="text-ink-100/40">because: </span>
          {entry.because}
        </div>
      ) : null}

      {entry.absorbed_labels && entry.absorbed_labels.length > 0 ? (
        <div className="mt-1">
          <div className="text-[10px] uppercase tracking-wide text-ink-100/40">
            folded in
          </div>
          <ul className="space-y-0.5 border-l border-white/10 pl-2 text-[10px] text-ink-100/50">
            {entry.absorbed_labels.map((label, i) => (
              <li key={i} className="whitespace-pre-wrap break-words">
                {label}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="mt-1 text-[10px] text-ink-100/40">
        {formatRelative(entry.at)}
        {entry.learning_event_ids && entry.learning_event_ids.length > 0 ? (
          <span
            className="ml-1"
            title="The recorded changes this line rests on. Their reasons are what she is allowed to say about it."
          >
            · {entry.learning_event_ids.length} recorded change
            {entry.learning_event_ids.length === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>
    </li>
  );
}
