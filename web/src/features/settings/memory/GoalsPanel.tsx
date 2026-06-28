import { useCallback, useMemo, useState } from "react";
import { api } from "../../../api";
import type { Memory } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

/**
 * K1 long-term goals panel.
 *
 * Shows Aiko's active long-term goals + her most recent reflection
 * note per goal. Mirrors the cooperative shape of
 * {@link CuriositySeedsPanel}: a "reflect now" button forces one
 * ``GoalWorker.run()`` so a tester can watch the cold-start bootstrap
 * fill the ring or watch a reflection note land on the oldest-touched
 * goal without waiting for the next hourly tick. Archived goals stay
 * in SQLite for audit but are hidden behind a toggle.
 */
export function GoalsPanel() {
  const [running, setRunning] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  const loader = useCallback(async () => {
    const [goalsRes, progressRes] = await Promise.all([
      api.listMemories({ kind: "goal", limit: 50, order: "recent" }),
      api.listMemories({
        kind: "goal_progress",
        limit: 100,
        order: "recent",
      }),
    ]);
    return {
      goals: (goalsRes.memories as Memory[]) || [],
      progress: (progressRes.memories as Memory[]) || [],
    };
  }, []);
  const {
    data: { goals, progress },
    loading,
    error,
    setError,
    refresh,
  } = useAsyncResource(loader, {
    goals: [] as Memory[],
    progress: [] as Memory[],
  });

  const onRun = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      await api.runGoalWorker();
      await refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setRunning(false);
    }
  }, [refresh, setError]);

  // Index the most-recent progress note per goal so the panel can
  // render a single "(recent: ...)" sub-line without scanning the
  // tail every render.
  const recentByGoal = useMemo(() => {
    const map = new Map<number, Memory>();
    for (const row of progress) {
      const meta = (row.metadata || {}) as Record<string, unknown>;
      const goalIdRaw = meta["goal_id"];
      const goalId =
        typeof goalIdRaw === "number"
          ? goalIdRaw
          : typeof goalIdRaw === "string"
            ? Number(goalIdRaw)
            : NaN;
      if (!Number.isFinite(goalId)) continue;
      const existing = map.get(goalId);
      if (!existing) {
        map.set(goalId, row);
        continue;
      }
      const ts = row.created_at || "";
      const existingTs = existing.created_at || "";
      if (ts > existingTs) map.set(goalId, row);
    }
    return map;
  }, [progress]);

  const visible = useMemo(() => {
    if (showArchived) return goals;
    return goals.filter((g) => {
      const meta = (g.metadata || {}) as Record<string, unknown>;
      return !meta["archived_at"];
    });
  }, [goals, showArchived]);

  const activeCount = useMemo(
    () =>
      goals.filter((g) => {
        const meta = (g.metadata || {}) as Record<string, unknown>;
        return !meta["archived_at"];
      }).length,
    [goals],
  );

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Aiko's quiet long-term goals -- the things she's slowly working toward across many sessions. Distinct from agenda TODOs the user gave her. Generated + reflected on by the K1 GoalWorker during idle windows."
        >
          Long-term goals
          <span className="ml-2 text-ink-100/40">({activeCount} active)</span>
        </span>
        <div className="flex items-center gap-2 text-ink-100/50">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />
            <span>show archived</span>
          </label>
          <RefreshButton
            onClick={onRun}
            loading={running || loading}
            label="reflect now"
            title="Force one GoalWorker.run() now (cold-start bootstrap if the ring is empty; otherwise one reflection note on the oldest-touched goal)."
          />
          <RefreshButton onClick={refresh} loading={loading} />
        </div>
      </div>
      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}
      {visible.length === 0 ? (
        <EmptyState>
          No active goals. The worker runs once an hour during idle
          windows; click "reflect now" to bootstrap immediately.
        </EmptyState>
      ) : (
        <ul className="space-y-1">
          {visible.map((goal) => {
            const meta = (goal.metadata || {}) as Record<string, unknown>;
            const summary =
              (typeof meta["summary"] === "string" && meta["summary"]) ||
              goal.content ||
              "(unnamed)";
            const archivedAt =
              typeof meta["archived_at"] === "string"
                ? (meta["archived_at"] as string)
                : null;
            const archived = Boolean(archivedAt);
            const lastReflection =
              typeof meta["last_progress_note"] === "string"
                ? (meta["last_progress_note"] as string)
                : "";
            const lastReflectedAt =
              typeof meta["last_reflected_at"] === "string"
                ? (meta["last_reflected_at"] as string)
                : null;
            const reflectionCount =
              typeof meta["reflection_count"] === "number"
                ? (meta["reflection_count"] as number)
                : 0;
            const source =
              typeof meta["source"] === "string"
                ? (meta["source"] as string)
                : null;
            const createdAt = goal.created_at || null;
            // Prefer the dedicated progress row's note when available
            // (carries the slightly longer note + accurate created_at)
            // but fall back to the goal's own mirror.
            const recentRow = recentByGoal.get(Number(goal.id));
            const recentNote =
              recentRow &&
              typeof (recentRow.metadata as Record<string, unknown>)["note"] ===
                "string"
                ? ((recentRow.metadata as Record<string, unknown>)[
                    "note"
                  ] as string)
                : lastReflection;
            const recentTs =
              recentRow?.created_at || lastReflectedAt || null;
            return (
              <li
                key={goal.id}
                className={`rounded border px-2 py-1.5 text-[11px] ${
                  archived
                    ? "border-white/5 bg-white/[0.02] text-ink-100/50"
                    : "border-white/5 bg-white/[0.03]"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={`font-medium ${
                      archived ? "line-through" : "text-ink-100/85"
                    }`}
                  >
                    {summary}
                  </span>
                  <span className="shrink-0 text-ink-100/40">
                    {formatRelative(createdAt)}
                    {archived ? (
                      <span className="ml-2 text-amber-300/80">
                        archived
                      </span>
                    ) : null}
                  </span>
                </div>
                {recentNote ? (
                  <p
                    className={`mt-0.5 italic text-ink-100/55 ${
                      archived ? "line-through" : ""
                    }`}
                    title={
                      recentTs
                        ? `reflected ${formatRelative(recentTs)}`
                        : undefined
                    }
                  >
                    recent: {recentNote}
                  </p>
                ) : null}
                {reflectionCount > 0 || source ? (
                  <p className="mt-0.5 text-[10px] text-ink-100/35">
                    {reflectionCount > 0 ? (
                      <span>{reflectionCount} reflection(s)</span>
                    ) : null}
                    {reflectionCount > 0 && source ? (
                      <span className="mx-1">•</span>
                    ) : null}
                    {source ? <span>source: {source}</span> : null}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
