import { useCallback, useMemo, useState } from "react";
import { api } from "../../../api";
import type { CueRow, CueState, CueTypeStats } from "../../../types";
import { CUE_STATES } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useCuePoolStore } from "@/stores/useCuePoolStore";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

/**
 * Everything Aiko is holding but hasn't said, and what became of it.
 *
 * This replaced the old curiosity-seeds panel when the seven cue
 * workers were consolidated onto one ``cue_pool`` table. The panel's
 * reason to exist is the column that view couldn't have: ``state``.
 * Before the pool, a cue was retired the moment its block rendered, so
 * "she used it" and "she ignored it" were the same row. Here a cue only
 * reaches ``used`` when post-turn matching found its subject in what
 * was actually said, which makes the ``used`` / ``expired`` split a
 * direct read on whether a cue type is landing.
 *
 * Terminal rows are never deleted, so the counts under each type
 * accumulate. ``mean_surfacings_before_use`` is the number to watch: a
 * type that routinely needs showing twice is one whose framing isn't
 * working.
 *
 * Live through ``cue_pool_updated`` — a cue flipping to ``used`` shows
 * up in the same beat Aiko uses it.
 */
const STATE_BADGE_CLASS: Record<CueState, string> = {
  pending: "bg-slate-500/15 text-slate-200",
  surfaced: "bg-violet-500/15 text-violet-200",
  awaiting: "bg-amber-500/15 text-amber-200",
  used: "bg-emerald-500/15 text-emerald-200",
  expired: "bg-zinc-500/15 text-zinc-200",
  superseded: "bg-zinc-500/15 text-zinc-200",
};

const STATE_HELP: Record<CueState, string> = {
  pending: "Waiting on the shelf. A worker wrote it; no prompt has carried it yet.",
  surfaced: "It reached her prompt this turn. Not the same as her using it.",
  awaiting: "She asked. Whether it counts depends on what you say next.",
  used: "She actually said it — post-turn matching found the subject in the transcript.",
  expired: "Offered too many times without being taken, or asked without an answer.",
  superseded: "A newer cue about the same subject replaced it.",
};

const LIVE_STATES = new Set<CueState>(["pending", "surfaced", "awaiting"]);

function StatePill({ state }: { state: CueState }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
        STATE_BADGE_CLASS[state] ?? "bg-white/5 text-ink-100/70"
      }`}
      title={STATE_HELP[state]}
    >
      {state}
    </span>
  );
}

function FilterPill({
  label,
  active,
  onClick,
  title,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={
        "rounded border px-1.5 py-0.5 " +
        (active
          ? "border-ink-400 bg-ink-400/10 text-ink-100"
          : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
      }
    >
      {label}
    </button>
  );
}

/** Per-type totals for the current pool, with the one derived number
 *  worth reading at a glance. Always over the whole pool, not the
 *  filtered page — it's the denominator the page is a slice of. */
function StatsStrip({ stats }: { stats: CueTypeStats[] }) {
  const live = stats.filter((s) => s.total > 0);
  if (live.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5 text-[10px] text-ink-100/45">
      {live.map((entry) => (
        <span
          key={entry.cue_type}
          className="rounded border border-white/5 bg-white/[0.02] px-1.5 py-0.5"
          title={
            `${entry.pending} waiting · ${entry.used} used · ` +
            `${entry.expired} expired` +
            (entry.mean_surfacings_before_use !== null
              ? ` · takes ${entry.mean_surfacings_before_use} surfacing(s) on average before she uses one`
              : "")
          }
        >
          <span className="text-ink-100/65">{entry.cue_type}</span>{" "}
          <span className="text-emerald-300/70">{entry.used}</span>
          <span className="text-ink-100/25">/</span>
          <span>{entry.total}</span>
          {entry.mean_surfacings_before_use !== null ? (
            <span className="ml-1 text-ink-100/35">
              ×{entry.mean_surfacings_before_use}
            </span>
          ) : null}
        </span>
      ))}
    </div>
  );
}

function CueCard({ cue }: { cue: CueRow }) {
  const spent = !LIVE_STATES.has(cue.state);
  const tone =
    cue.state === "used"
      ? "border-emerald-400/30 bg-emerald-500/5"
      : spent
        ? "border-white/5 bg-white/[0.02] opacity-70"
        : "border-white/5 bg-white/[0.03]";
  return (
    <li className={`rounded border px-2 py-1.5 text-[11px] ${tone}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <StatePill state={cue.state} />
          <span className="truncate font-medium text-ink-100/85">
            {cue.subject || "(no subject)"}
          </span>
        </div>
        <span className="shrink-0 text-ink-100/40">
          {formatRelative(cue.used_at || cue.last_surfaced_at || cue.created_at)}
        </span>
      </div>
      {cue.text ? (
        <p className="mt-0.5 italic text-ink-100/55">{cue.text}</p>
      ) : null}
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-ink-100/35">
        <span>{cue.cue_type}</span>
        {cue.surfaced_count > 0 ? (
          <span title="Turns this cue sat in her prompt and she didn't raise it.">
            passed over {cue.surfaced_count}×
          </span>
        ) : null}
        {cue.ask_count > 0 ? (
          <span title="Times she raised it and the answer never came.">
            asked {cue.ask_count}×
          </span>
        ) : null}
        {cue.used_evidence ? (
          <span
            className="font-mono"
            title="How the match was made, or why the cue died."
          >
            {cue.used_evidence}
          </span>
        ) : null}
      </div>
    </li>
  );
}

export function CuesPanel() {
  const view = useCuePoolStore((s) => s.cuePoolView);
  const enabled = useCuePoolStore((s) => s.cuePoolEnabled);
  const setView = useCuePoolStore((s) => s.setCuePoolView);
  const setPage = useCuePoolStore((s) => s.setCuePoolPage);
  const setTypeFilter = useCuePoolStore((s) => s.setCuePoolTypeFilter);
  const setStateFilter = useCuePoolStore((s) => s.setCuePoolStateFilter);
  const [running, setRunning] = useState(false);

  const { page, pageSize, typeFilter, stateFilter } = view;
  const loader = useCallback(async () => {
    const res = await api.listCuePool({
      limit: pageSize,
      offset: page * pageSize,
      cueType: typeFilter,
      state: stateFilter,
    });
    setView({
      items: res.cues,
      total: res.total,
      stats: res.stats,
      types: res.types,
      enabled: res.enabled,
    });
  }, [page, pageSize, typeFilter, stateFilter, setView]);
  const { loading, error, setError, refresh } = useAsyncResource<void>(
    loader,
    undefined,
  );

  const onRun = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      await api.runCuriositySeedWorker();
      await refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setRunning(false);
    }
  }, [refresh, setError]);

  const waiting = useMemo(
    () => view.stats.reduce((sum, entry) => sum + entry.pending, 0),
    [view.stats],
  );
  const pageCount = Math.max(1, Math.ceil(view.total / pageSize));

  if (!enabled) {
    return (
      <Panel className="text-[11px] text-ink-100/40">
        The cue pool is unavailable (the store failed to initialise). Check
        the backend logs.
      </Panel>
    );
  }

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Conversational moves Aiko is holding but hasn't made: topics gone quiet, gaps she noticed, associations she wants to follow. Written by the idle workers; a cue only reaches 'used' when she actually says the thing."
        >
          Cues
          <span className="ml-2 text-ink-100/40">({waiting} waiting)</span>
        </span>
        <div className="flex items-center gap-2 text-ink-100/50">
          <RefreshButton
            onClick={onRun}
            loading={running}
            label="seed now"
            title="Force one CuriositySeedWorker.run() now (instead of waiting for the next idle tick)."
          />
          <RefreshButton onClick={refresh} loading={loading} />
        </div>
      </div>

      <StatsStrip stats={view.stats} />

      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>type:</span>
        <FilterPill
          label="all"
          active={typeFilter === null}
          onClick={() => setTypeFilter(null)}
        />
        {view.types.map((cueType) => (
          <FilterPill
            key={cueType}
            label={cueType}
            active={typeFilter === cueType}
            onClick={() => setTypeFilter(cueType)}
          />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>status:</span>
        <FilterPill
          label="all"
          active={stateFilter === null}
          onClick={() => setStateFilter(null)}
        />
        {CUE_STATES.map((cueState) => (
          <FilterPill
            key={cueState}
            label={cueState}
            active={stateFilter === cueState}
            onClick={() => setStateFilter(cueState)}
            title={STATE_HELP[cueState]}
          />
        ))}
      </div>

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {view.items.length === 0 ? (
        <EmptyState>
          {loading
            ? "loading cues…"
            : typeFilter || stateFilter
              ? "No cues match this filter."
              : "Nothing on the shelf. The cue workers run during idle windows; click \"seed now\" to force one immediately."}
        </EmptyState>
      ) : (
        <ul className="space-y-1">
          {view.items.map((cue) => (
            <CueCard key={cue.id} cue={cue} />
          ))}
        </ul>
      )}

      {pageCount > 1 ? (
        <div className="flex items-center justify-center gap-3 pt-1 text-[11px] text-ink-100/60">
          <button
            type="button"
            onClick={() => setPage(page - 1)}
            disabled={loading || page <= 0}
            className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Prev
          </button>
          <span className="font-mono text-ink-100/40">
            page {page + 1} of {pageCount}
          </span>
          <button
            type="button"
            onClick={() => setPage(page + 1)}
            disabled={loading || page + 1 >= pageCount}
            className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
          </button>
        </div>
      ) : null}
    </Panel>
  );
}
