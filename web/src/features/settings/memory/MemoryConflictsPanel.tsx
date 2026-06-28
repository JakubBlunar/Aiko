import { useCallback, useState } from "react";
import { api } from "../../../api";
import type {
  Memory,
  MemoryConflictPair,
  MemoryConflictsResponse,
} from "../../../types";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

export function MemoryConflictsPanel() {
  const [showResolved, setShowResolved] = useState(false);

  const loader = useCallback(
    () => api.listMemoryConflicts({ limit: 50, includeRecent: true }),
    [],
  );
  const { data, loading, error, setError, refresh } =
    useAsyncResource<MemoryConflictsResponse | null>(loader, null);

  const onResolve = useCallback(
    async (pair: MemoryConflictPair, winnerId: number) => {
      try {
        await api.resolveMemoryConflict(pair.id, {
          winner_id: winnerId,
          action: "demote",
        });
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const onDismiss = useCallback(
    async (pair: MemoryConflictPair) => {
      try {
        await api.dismissMemoryConflict(pair.id);
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const open = data?.open ?? [];
  const resolved = data?.recently_auto_resolved ?? [];
  const counts = data?.counts ?? {
    open: 0,
    auto_resolved: 0,
    user_resolved: 0,
    dismissed: 0,
  };

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Pairs of memories the F5 detector flagged as contradicting. Pick which side to keep -- the loser is moved to archive at low confidence so RAG stops surfacing it."
        >
          Conflicts
          <span className="ml-2 text-ink-100/40">({counts.open})</span>
        </span>
        <RefreshButton onClick={refresh} loading={loading} />
      </div>
      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}
      {open.length === 0 ? (
        <EmptyState>
          No open conflicts. Aiko's F5 detector will flag pairs here when
          two memories disagree about the same topic.
        </EmptyState>
      ) : (
        <ul className="space-y-2">
          {open.map((pair) => (
            <ConflictPairCard
              key={pair.id}
              pair={pair}
              onResolve={onResolve}
              onDismiss={onDismiss}
            />
          ))}
        </ul>
      )}
      {resolved.length > 0 ? (
        <div className="mt-2 rounded border border-white/5 bg-white/[0.02] p-2 text-[11px]">
          <button
            type="button"
            onClick={() => setShowResolved((v) => !v)}
            className="flex w-full items-center justify-between text-left text-ink-100/60 hover:text-ink-100"
          >
            <span>
              Recently auto-resolved
              <span className="ml-2 text-ink-100/40">
                ({counts.auto_resolved})
              </span>
            </span>
            <span className="text-ink-100/40">
              {showResolved ? "hide" : "show"}
            </span>
          </button>
          {showResolved ? (
            <ul className="mt-2 space-y-1">
              {resolved.map((pair) => (
                <li
                  key={pair.id}
                  className="rounded border border-white/5 bg-white/[0.02] px-2 py-1 text-ink-100/50"
                >
                  <div className="text-[10px] uppercase text-ink-100/40">
                    auto-demoted #{pair.loser_id} · kept #{pair.winner_id}
                    {" · "}sim {pair.similarity.toFixed(2)} · Δconf{" "}
                    {pair.confidence_delta.toFixed(2)}
                  </div>
                  <div className="text-ink-100/70">
                    A: {pair.memory_a?.content ?? "(deleted)"}
                  </div>
                  <div className="text-ink-100/70">
                    B: {pair.memory_b?.content ?? "(deleted)"}
                  </div>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </Panel>
  );
}

interface ConflictPairCardProps {
  pair: MemoryConflictPair;
  onResolve: (pair: MemoryConflictPair, winnerId: number) => void | Promise<void>;
  onDismiss: (pair: MemoryConflictPair) => void | Promise<void>;
}

function ConflictPairCard({
  pair,
  onResolve,
  onDismiss,
}: ConflictPairCardProps) {
  const a = pair.memory_a;
  const b = pair.memory_b;
  return (
    <li className="rounded border border-amber-400/30 bg-amber-500/5 p-2 text-[11px]">
      <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-amber-200/80">
        <span>
          sim {pair.similarity.toFixed(2)}
        </span>
        <span>·</span>
        <span>{pair.heuristic_label}</span>
        {pair.heuristic_signals.length > 0 ? (
          <>
            <span>·</span>
            {pair.heuristic_signals.map((signal) => (
              <span
                key={signal}
                className="rounded bg-amber-500/10 px-1 py-px text-[9px] normal-case"
              >
                {signal}
              </span>
            ))}
          </>
        ) : null}
        {pair.llm_verdict ? (
          <>
            <span>·</span>
            <span>LLM: {pair.llm_verdict}</span>
          </>
        ) : null}
        {pair.flagged_by === "aiko" ? (
          <span className="rounded bg-violet-500/30 px-1 text-[9px] text-violet-100">
            aiko-flagged
          </span>
        ) : null}
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <ConflictMemorySide
          memory={a}
          isWinner={false}
          onPick={() => onResolve(pair, pair.memory_a_id)}
        />
        <ConflictMemorySide
          memory={b}
          isWinner={false}
          onPick={() => onResolve(pair, pair.memory_b_id)}
        />
      </div>
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          onClick={() => onDismiss(pair)}
          className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
          title="Mark as not actually a conflict; keep both memories untouched."
        >
          not a conflict
        </button>
      </div>
    </li>
  );
}

interface ConflictMemorySideProps {
  memory: Memory | null;
  isWinner: boolean;
  onPick: () => void | Promise<void>;
}

function ConflictMemorySide({
  memory,
  onPick,
}: ConflictMemorySideProps) {
  if (memory === null) {
    return (
      <div className="rounded border border-white/5 bg-white/[0.03] p-2 text-ink-100/50 italic">
        (memory missing)
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1 rounded border border-white/5 bg-white/[0.03] p-2">
      <div className="text-[10px] uppercase tracking-wide text-ink-100/40">
        #{memory.id} · {memory.kind} · conf{" "}
        {memory.confidence?.toFixed(2) ?? "—"}
      </div>
      <div className="text-ink-100/90">{memory.content}</div>
      <button
        type="button"
        onClick={onPick}
        className="mt-1 self-end rounded border border-emerald-400/40 px-2 py-0.5 text-[11px] text-emerald-200 hover:bg-emerald-500/10"
        title="Keep this side; the other becomes archived at low confidence."
      >
        keep this
      </button>
    </div>
  );
}
