import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../../api";
import { useAssistantStore } from "../../../store";
import type { AgendaItem, AgendaStatus } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";

/**
 * Phase 4a agenda panel (I3).
 *
 * The agenda is Aiko's medium-term roster of things-in-flight ("I want
 * to learn rust", "we should plan that trip"). Items arrive three ways:
 * inline ``[[agenda:...]]`` tags in her replies, the LLM grooming worker,
 * and manual adds here. The list stays live through the ``agenda_updated``
 * WS event (``applyAgendaUpdated`` upserts by id), so this panel only owns
 * the transient request status + the (client-side) status filter.
 */
const STATUS_FILTERS: ReadonlyArray<{ id: AgendaStatus | "all"; label: string }> = [
  { id: "open", label: "Open" },
  { id: "done", label: "Done" },
  { id: "dropped", label: "Dropped" },
  { id: "snoozed", label: "Snoozed" },
  { id: "all", label: "All" },
];

export function AgendaPanel() {
  const agendaView = useAssistantStore((s) => s.agendaView);
  const setAgendaView = useAssistantStore((s) => s.setAgendaView);
  const applyAgendaUpdated = useAssistantStore((s) => s.applyAgendaUpdated);
  const [statusFilter, setStatusFilter] = useState<AgendaStatus | "all">("open");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newGoal, setNewGoal] = useState("");
  const [adding, setAdding] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Fetch the full roster once; status filtering happens client-side
      // so the live WS upserts never need a refetch on a status flip.
      const res = await api.listAgenda({ status: "all", limit: 100 });
      setAgendaView({ items: res.items, enabled: res.enabled });
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [setAgendaView]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onAdd = useCallback(async () => {
    const goal = newGoal.trim();
    if (goal.length < 3) return;
    setAdding(true);
    setError(null);
    try {
      const res = await api.createAgenda({ goal });
      applyAgendaUpdated(res.item);
      setNewGoal("");
    } catch (err) {
      setError(String(err));
    } finally {
      setAdding(false);
    }
  }, [newGoal, applyAgendaUpdated]);

  const mutate = useCallback(
    async (
      id: number,
      patch: { status?: AgendaStatus; importance?: number; goal?: string },
    ) => {
      try {
        const res = await api.updateAgenda(id, patch);
        applyAgendaUpdated(res.item);
      } catch (err) {
        setError(String(err));
      }
    },
    [applyAgendaUpdated],
  );

  const items = agendaView.items;
  const enabled = agendaView.enabled;
  const visible = useMemo(() => {
    const filtered =
      statusFilter === "all"
        ? items
        : items.filter((a) => a.status === statusFilter);
    // Open-first, then by importance desc, then most-recent.
    return filtered.slice().sort((a, b) => {
      if (a.status !== b.status) {
        if (a.status === "open") return -1;
        if (b.status === "open") return 1;
      }
      if (b.importance !== a.importance) return b.importance - a.importance;
      return (b.created_at || "").localeCompare(a.created_at || "");
    });
  }, [items, statusFilter]);

  const openCount = useMemo(
    () => items.filter((a) => a.status === "open").length,
    [items],
  );

  if (!enabled) {
    return (
      <Panel className="text-[11px] text-ink-100/40">
        Agenda is unavailable (the store failed to initialise). Check the
        backend logs.
      </Panel>
    );
  }

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Aiko's medium-term roster of things-in-flight. Distinct from her quiet long-term goals: agenda items are concrete intentions the two of you are tracking. Filled by inline tags in her replies, the grooming worker, or manual adds here."
        >
          Agenda
          <span className="ml-2 text-ink-100/40">({openCount} open)</span>
        </span>
        <RefreshButton onClick={refresh} loading={loading} />
      </div>

      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>status:</span>
        {STATUS_FILTERS.map((opt) => (
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

      <div className="flex items-center gap-1">
        <input
          type="text"
          value={newGoal}
          onChange={(e) => setNewGoal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void onAdd();
          }}
          placeholder="Add an agenda item…"
          className="flex-1 rounded border border-white/10 bg-white/[0.03] px-2 py-1 text-[11px] text-ink-100/90 placeholder:text-ink-100/30 focus:border-ink-400/60 focus:outline-none"
        />
        <button
          type="button"
          onClick={() => void onAdd()}
          disabled={adding || newGoal.trim().length < 3}
          className="rounded border border-white/10 px-2 py-1 text-[11px] hover:border-ink-400 disabled:opacity-40"
        >
          {adding ? "..." : "add"}
        </button>
      </div>

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {visible.length === 0 ? (
        <EmptyState>
          Nothing here. Aiko adds agenda items as you talk; you can also add
          one above.
        </EmptyState>
      ) : (
        <ul className="space-y-1">
          {visible.map((item) => (
            <AgendaCard key={item.id} item={item} onMutate={mutate} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

interface AgendaCardProps {
  item: AgendaItem;
  onMutate: (
    id: number,
    patch: { status?: AgendaStatus; importance?: number; goal?: string },
  ) => void | Promise<void>;
}

function AgendaCard({ item, onMutate }: AgendaCardProps) {
  const tone =
    item.status === "done"
      ? "border-emerald-400/30 bg-emerald-500/5 opacity-70"
      : item.status === "dropped"
      ? "border-white/10 bg-white/[0.02] opacity-60"
      : item.status === "snoozed"
      ? "border-sky-400/30 bg-sky-500/5"
      : "border-white/5 bg-white/[0.03]";
  const struck = item.status === "done" || item.status === "dropped";
  return (
    <li className={`rounded border p-2 text-[11px] ${tone}`}>
      <div className="flex items-start justify-between gap-2">
        <span className={`text-ink-100/85 ${struck ? "line-through" : ""}`}>
          {item.goal}
        </span>
        <span className="shrink-0 text-[10px] text-ink-100/40">
          {formatRelative(item.created_at)}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/50">
        <span>{item.status}</span>
        <span>·</span>
        <span>importance {item.importance.toFixed(2)}</span>
        {item.due_at ? (
          <>
            <span>·</span>
            <span>due {formatRelative(item.due_at)}</span>
          </>
        ) : null}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1 text-[10px]">
        {item.status !== "done" ? (
          <button
            type="button"
            onClick={() => void onMutate(item.id, { status: "done" })}
            className="rounded border border-white/10 px-1.5 py-0.5 hover:border-emerald-300 hover:text-emerald-200"
          >
            complete
          </button>
        ) : null}
        {item.status !== "dropped" ? (
          <button
            type="button"
            onClick={() => void onMutate(item.id, { status: "dropped" })}
            className="rounded border border-white/10 px-1.5 py-0.5 hover:border-rose-300 hover:text-rose-200"
          >
            drop
          </button>
        ) : null}
        {item.status !== "open" ? (
          <button
            type="button"
            onClick={() => void onMutate(item.id, { status: "open" })}
            className="rounded border border-white/10 px-1.5 py-0.5 hover:border-ink-400 hover:text-ink-100"
          >
            reopen
          </button>
        ) : null}
      </div>
    </li>
  );
}
