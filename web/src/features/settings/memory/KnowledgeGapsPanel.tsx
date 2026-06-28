import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api";
import type { Memory } from "../../../types";

export interface KnowledgeGapRow extends Memory {
  metadata?: {
    topic?: string;
    question?: string;
    resolved_at?: string | null;
    resolved_by_memory_id?: number | null;
    flags?: { conflict?: boolean };
    [key: string]: unknown;
  };
}

export function KnowledgeGapsPanel() {
  const [gaps, setGaps] = useState<KnowledgeGapRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [includeResolved, setIncludeResolved] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listKnowledgeGaps(includeResolved);
      setGaps((data.gaps as KnowledgeGapRow[]) || []);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [includeResolved]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onDismiss = useCallback(async (id: number) => {
    try {
      await api.deleteKnowledgeGap(id);
      setGaps((rows) => rows.filter((r) => r.id !== id));
    } catch (err) {
      setError(String(err));
    }
  }, []);

  const onResolve = useCallback(
    async (id: number) => {
      const answer = window.prompt(
        "Quick answer (optional). Leave blank to just dismiss without writing a memory:",
        "",
      );
      if (answer === null) return;
      try {
        await api.resolveKnowledgeGap(id, answer.trim() || undefined);
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Open questions Aiko emitted via [[gap:topic:question]] tags. F1's background fact-checker may resolve them automatically; otherwise dismiss or answer manually."
        >
          Things I'm not sure about
          <span className="ml-2 text-ink-100/40">({gaps.length})</span>
        </span>
        <div className="flex items-center gap-2 text-ink-100/50">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={includeResolved}
              onChange={(e) => setIncludeResolved(e.target.checked)}
            />
            <span>show resolved</span>
          </label>
          <button
            type="button"
            onClick={refresh}
            disabled={loading}
            className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
          >
            {loading ? "..." : "refresh"}
          </button>
        </div>
      </div>
      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}
      {gaps.length === 0 ? (
        <p className="text-[11px] text-ink-100/40">
          No open questions. Aiko will jot uncertainties here as
          [[gap:topic:question]] tags from her replies.
        </p>
      ) : (
        <ul className="space-y-1">
          {gaps.map((gap) => {
            const meta = gap.metadata || {};
            const topic = typeof meta.topic === "string" ? meta.topic : "";
            const question =
              typeof meta.question === "string"
                ? meta.question
                : (gap.content || "").trim();
            const resolved = Boolean(meta.resolved_at);
            return (
              <li
                key={gap.id}
                className={`flex items-start justify-between gap-2 rounded border px-2 py-1.5 text-[11px] ${
                  resolved
                    ? "border-emerald-400/30 bg-emerald-500/5 text-ink-100/60"
                    : "border-white/5 bg-white/[0.03]"
                }`}
              >
                <div className="min-w-0 flex-1">
                  {topic ? (
                    <span className="mr-1 inline-block rounded bg-white/10 px-1 text-ink-100/70 uppercase tracking-wide">
                      {topic}
                    </span>
                  ) : null}
                  <span className={resolved ? "line-through" : ""}>
                    {question}
                  </span>
                  {resolved ? (
                    <span className="ml-2 text-emerald-300/80">resolved</span>
                  ) : null}
                </div>
                {!resolved ? (
                  <div className="flex shrink-0 gap-1">
                    <button
                      type="button"
                      onClick={() => onResolve(gap.id)}
                      className="rounded border border-emerald-400/40 px-1.5 py-0.5 text-emerald-200 hover:bg-emerald-500/10"
                      title="Mark this gap resolved. You can optionally provide a short answer that will be written as a memory."
                    >
                      answer
                    </button>
                    <button
                      type="button"
                      onClick={() => onDismiss(gap.id)}
                      className="rounded border border-white/10 px-1.5 py-0.5 text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                    >
                      dismiss
                    </button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
