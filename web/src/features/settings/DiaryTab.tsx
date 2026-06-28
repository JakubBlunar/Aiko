import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { Memory } from "../../types";
import { stripJournalPrefix, type JournalBadge } from "../../lib/journalText";
import { Section } from "./SettingsSection";

const DIARY_PAGE_SIZE = 30;

// The journal kinds the diary surfaces. ``null`` = everything. Dreams
// and mindmap noticings aren't separate kinds (they're ``reflection``
// rows distinguished by a content prefix) so they're filtered by badge
// on top of the kind filter below.
const KIND_FILTERS: ReadonlyArray<{ id: string | null; label: string }> = [
  { id: null, label: "everything" },
  { id: "diary", label: "entries" },
  { id: "reflection", label: "reflections" },
  { id: "shared_moment", label: "moments" },
  { id: "open_question", label: "questions" },
];

const BADGE_STYLES: Record<Exclude<JournalBadge, null>, { label: string; cls: string; title: string }> = {
  dream: {
    label: "dream",
    cls: "bg-indigo-500/20 text-indigo-200",
    title: "A between-sessions dream.",
  },
  noticing: {
    label: "noticing",
    cls: "bg-violet-500/20 text-violet-200",
    title: "A noticing about the shape of what she knows.",
  },
};

// A per-kind badge for the rows that aren't dream/mindmap reflections,
// so a moment or open question reads as what it is.
function kindBadge(kind: string): { label: string; cls: string } | null {
  if (kind === "diary") {
    return { label: "diary entry", cls: "bg-emerald-500/20 text-emerald-200" };
  }
  if (kind === "shared_moment") {
    return { label: "moment", cls: "bg-rose-500/20 text-rose-200" };
  }
  if (kind === "open_question") {
    return { label: "wondering", cls: "bg-amber-500/20 text-amber-200" };
  }
  if (kind === "reflection") {
    return { label: "reflection", cls: "bg-sky-500/15 text-sky-200" };
  }
  return null;
}

function dayKey(iso: string): string {
  // Group by the local calendar day. Falls back to the raw string when
  // the timestamp is unparseable so a bad row never collapses the list.
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10) || "unknown";
  return d.toLocaleDateString(undefined, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function entryTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

interface DayGroup {
  day: string;
  entries: Memory[];
}

export function DiaryTab() {
  const [entries, setEntries] = useState<Memory[]>([]);
  const [total, setTotal] = useState(0);
  const [enabled, setEnabled] = useState(true);
  const [page, setPage] = useState(0);
  const [kind, setKind] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getDiary({
        limit: DIARY_PAGE_SIZE,
        offset: page * DIARY_PAGE_SIZE,
        kind,
      });
      setEntries(data.entries || []);
      setTotal(data.total || 0);
      setEnabled(Boolean(data.enabled));
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [page, kind]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const groups = useMemo<DayGroup[]>(() => {
    const out: DayGroup[] = [];
    let current: DayGroup | null = null;
    for (const entry of entries) {
      const day = dayKey(entry.created_at);
      if (!current || current.day !== day) {
        current = { day, entries: [] };
        out.push(current);
      }
      current.entries.push(entry);
    }
    return out;
  }, [entries]);

  const pageCount = Math.max(1, Math.ceil(total / DIARY_PAGE_SIZE));

  if (!enabled) {
    return (
      <Section title="Diary">
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          Long-term memory is disabled in config (memory.enabled), so there's
          no diary to read yet.
        </p>
      </Section>
    );
  }

  return (
    <Section title="Diary">
      <p className="text-[11px] text-ink-100/50">
        A window into Aiko's inner life — the reflections, dreams, and little
        noticings she writes between your conversations. Read-only; this is her
        journal, in her own words.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1">
          {KIND_FILTERS.map((f) => {
            const isActive = kind === f.id;
            return (
              <button
                key={f.label}
                type="button"
                onClick={() => {
                  setKind(f.id);
                  setPage(0);
                }}
                aria-pressed={isActive}
                className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition ${
                  isActive
                    ? "bg-ink-500/30 text-ink-100 ring-1 ring-ink-400/50"
                    : "text-ink-100/55 hover:bg-white/5 hover:text-ink-100/90"
                }`}
              >
                {f.label}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          onClick={() => {
            void refresh();
          }}
          disabled={loading}
          className="ml-auto rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? "Loading..." : "Refresh"}
        </button>
      </div>

      {error ? (
        <div className="mt-2 rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      ) : null}

      {entries.length === 0 ? (
        <p className="mt-3 rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          Nothing here yet. As you talk, Aiko jots down reflections, the odd
          dream, and moments worth keeping — they'll appear here over time.
        </p>
      ) : (
        <div className="mt-3 space-y-4">
          {groups.map((group) => (
            <div key={group.day}>
              <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-100/45">
                {group.day}
              </h4>
              <ul className="space-y-1.5">
                {group.entries.map((entry) => {
                  const { text, badge } = stripJournalPrefix(entry.content);
                  const badgeStyle = badge ? BADGE_STYLES[badge] : null;
                  const kBadge = badge ? null : kindBadge(entry.kind);
                  const time = entryTime(entry.created_at);
                  return (
                    <li
                      key={entry.id}
                      className={`rounded-md border px-3 py-2 text-xs ${
                        entry.pinned
                          ? "border-amber-400/40 bg-amber-500/5"
                          : "border-white/5 bg-white/[0.03]"
                      }`}
                    >
                      <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wide">
                        {badgeStyle ? (
                          <span
                            className={`rounded px-1.5 py-0.5 ${badgeStyle.cls}`}
                            title={badgeStyle.title}
                          >
                            {badgeStyle.label}
                          </span>
                        ) : kBadge ? (
                          <span className={`rounded px-1.5 py-0.5 ${kBadge.cls}`}>
                            {kBadge.label}
                          </span>
                        ) : null}
                        {time ? (
                          <span className="text-ink-100/35">{time}</span>
                        ) : null}
                        {entry.pinned ? (
                          <span className="text-amber-300/70">pinned</span>
                        ) : null}
                      </div>
                      <p className="break-words italic text-ink-100/90">{text}</p>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      )}

      {pageCount > 1 ? (
        <div className="flex items-center justify-center gap-3 pt-3 text-[11px] text-ink-100/60">
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(0, p - 1))}
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
            onClick={() => setPage((p) => (p + 1 >= pageCount ? p : p + 1))}
            disabled={loading || page + 1 >= pageCount}
            className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
          </button>
        </div>
      ) : null}
    </Section>
  );
}
