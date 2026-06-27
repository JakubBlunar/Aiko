import { useMemo, useState } from "react";
import type { Memory, MemoryOrder, MemoryTier } from "../../types";
import { MEMORY_KINDS, MEMORY_TIERS } from "../../types";
import { stripJournalPrefix } from "../../lib/journalText";
import { Section } from "./SettingsSection";
import { KnowledgeGapsPanel } from "./memory/KnowledgeGapsPanel";
import { MemoryConflictsPanel } from "./memory/MemoryConflictsPanel";
import { BeliefsPanel } from "./memory/BeliefsPanel";
import { CuriositySeedsPanel } from "./memory/CuriositySeedsPanel";
import { GoalsPanel } from "./memory/GoalsPanel";
import { TopicGraphPanel } from "./memory/TopicGraphPanel";
import { FactCheckerStatusFooter } from "./memory/FactCheckerStatusFooter";

export interface MemoryDraft {
  content: string;
  kind: string;
  salience: number;
}

// Schema v9 — confidence filter for the Memory tab. Pure client-side
// filter applied to the rendered page; doesn't change the API query
// (so per-tier totals in the header stay accurate). "Conflicted" is
// derived from ``metadata.flags.conflict`` (set by F1's fact-checker
// on contradiction).
type ConfidenceBand = "all" | "high" | "medium" | "low" | "conflicted";

const CONFIDENCE_BANDS: ReadonlyArray<{ id: ConfidenceBand; label: string }> = [
  { id: "all", label: "all" },
  { id: "high", label: "high (≥0.85)" },
  { id: "medium", label: "medium (0.5–0.85)" },
  { id: "low", label: "low (<0.5)" },
  { id: "conflicted", label: "conflicted" },
];

// The Memory tab used to stack every panel vertically, which meant a lot
// of scrolling to reach Beliefs / Topics / Goals at the bottom. We split
// it into sub-tabs: the main memory list plus one tab per panel. The
// FactChecker status footer stays persistent below all sub-tabs.
type MemorySubTab =
  | "memories"
  | "gaps"
  | "conflicts"
  | "beliefs"
  | "curiosity"
  | "topics"
  | "goals";

const MEMORY_SUB_TABS: ReadonlyArray<{ id: MemorySubTab; label: string }> = [
  { id: "memories", label: "Memories" },
  { id: "gaps", label: "Knowledge gaps" },
  { id: "conflicts", label: "Conflicts" },
  { id: "beliefs", label: "Beliefs" },
  { id: "curiosity", label: "Curiosity" },
  { id: "topics", label: "Topics" },
  { id: "goals", label: "Goals" },
];

function memoryIsConflicted(memory: Memory): boolean {
  const flags = (memory.metadata as { flags?: { conflict?: unknown } } | undefined)?.flags;
  return Boolean(flags?.conflict);
}

function memoryMatchesConfidenceBand(memory: Memory, band: ConfidenceBand): boolean {
  if (band === "all") return true;
  if (band === "conflicted") return memoryIsConflicted(memory);
  const value = typeof memory.confidence === "number" ? memory.confidence : 0.7;
  if (band === "high") return value >= 0.85;
  if (band === "medium") return value >= 0.5 && value < 0.85;
  if (band === "low") return value < 0.5;
  return true;
}

interface ConfidencePipProps {
  confidence: number | undefined;
  conflicted: boolean;
  verifiedAt?: string | null;
}

function memoryVerifiedAt(item: Memory): string | null {
  const metadata = (item.metadata ?? {}) as Record<string, unknown>;
  const value = metadata["last_verified_at"];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function ConfidencePip({ confidence, conflicted, verifiedAt }: ConfidencePipProps) {
  const value = typeof confidence === "number" ? confidence : 0.7;
  const pct = Math.round(value * 100);
  if (conflicted) {
    return (
      <span
        className="rounded bg-rose-500/20 px-1.5 py-0.5 text-rose-200"
        title={`Confidence ${pct}% · F1 fact-checker flagged a conflict (metadata.flags.conflict).`}
      >
        conflict · {pct}%
      </span>
    );
  }
  let cls = "rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-200";
  let label = "high";
  if (value < 0.5) {
    cls = "rounded bg-rose-500/15 px-1.5 py-0.5 text-rose-200";
    label = "low";
  } else if (value < 0.85) {
    cls = "rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-200";
    label = "med";
  }
  // Surface the F1 verified state next to high-confidence rows so the
  // user can tell apart "Aiko just believes this" from "an outside
  // source confirmed it within the last verify pass".
  const verifiedBadge = verifiedAt && value >= 0.85 ? (
    <span
      className="ml-1 rounded bg-emerald-500/25 px-1 py-0.5 text-[10px] text-emerald-100"
      title={`Verified by F1 fact-checker at ${verifiedAt}.`}
    >
      ✓
    </span>
  ) : null;
  return (
    <span
      className={cls}
      title={
        `Confidence ${pct}%. ` +
        "<0.5 demotes the memory in RAG and tags it (uncertain) in the prompt. " +
        "F1's background fact-checker pushes this up on positive verification."
      }
    >
      {label} · {pct}%
      {verifiedBadge}
    </span>
  );
}

export interface MemoryTabProps {
  view: {
    items: Memory[];
    total: number;
    cap: number;
    page: number;
    pageSize: number;
    kindFilter: string | null;
    tierFilter: MemoryTier | null;
    order: MemoryOrder;
    counts: { scratchpad: number; long_term: number; archive: number; total: number } | null;
  };
  enabled: boolean;
  busy: boolean;
  error: string | null;
  pageCount: number;
  rangeLabel: string;
  editingId: number | null;
  draft: MemoryDraft;
  setDraft: (draft: MemoryDraft) => void;
  newOpen: boolean;
  setNewOpen: (open: boolean) => void;
  newDraft: MemoryDraft;
  setNewDraft: (draft: MemoryDraft) => void;
  onSetKindFilter: (kind: string | null) => void;
  onSetTierFilter: (tier: MemoryTier | null) => void;
  onSetOrder: (order: MemoryOrder) => void;
  onSetPage: (page: number) => void;
  onRefresh: () => void;
  onStartEdit: (memory: Memory) => void;
  onCancelEdit: () => void;
  onSaveEdit: (memory: Memory) => void;
  onPin: (memory: Memory, pinned: boolean) => void;
  onDelete: (memory: Memory) => void;
  onCreate: () => void;
}

export function MemoryTab({
  view,
  enabled,
  busy,
  error,
  pageCount,
  rangeLabel,
  editingId,
  draft,
  setDraft,
  newOpen,
  setNewOpen,
  newDraft,
  setNewDraft,
  onSetKindFilter,
  onSetTierFilter,
  onSetOrder,
  onSetPage,
  onRefresh,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onPin,
  onDelete,
  onCreate,
}: MemoryTabProps) {
  // Schema v9 — confidence band filter. Pure client-side post-fetch
  // filter so we don't need backend query support for it; per-tier
  // totals stay accurate because the API call is unchanged.
  const [confidenceBand, setConfidenceBand] = useState<ConfidenceBand>("all");
  const [subTab, setSubTab] = useState<MemorySubTab>("memories");
  const visibleItems = useMemo(
    () => view.items.filter((m) => memoryMatchesConfidenceBand(m, confidenceBand)),
    [view.items, confidenceBand],
  );
  if (!enabled) {
    return (
      <Section title="Memory">
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          Long-term memory is disabled in config (memory.enabled).
        </p>
      </Section>
    );
  }

  return (
    <Section title="Memory">
      <nav
        className="flex flex-wrap gap-1 border-b border-white/5 pb-2"
        aria-label="Memory sections"
      >
        {MEMORY_SUB_TABS.map((t) => {
          const isActive = subTab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setSubTab(t.id)}
              aria-pressed={isActive}
              className={`shrink-0 rounded-md px-2.5 py-1 text-[11px] font-medium transition ${
                isActive
                  ? "bg-ink-500/30 text-ink-100 ring-1 ring-ink-400/50"
                  : "text-ink-100/55 hover:bg-white/5 hover:text-ink-100/90"
              }`}
            >
              {t.label}
            </button>
          );
        })}
      </nav>

      {subTab === "memories" ? (
        <div className="space-y-2">
      <div className="flex items-center justify-between gap-2 text-[11px] text-ink-100/50">
        <span>
          Showing {rangeLabel}
          {view.cap ? (
            <span className="text-ink-100/30"> · cap {view.cap}</span>
          ) : null}
        </span>
        <button
          type="button"
          onClick={onRefresh}
          disabled={busy}
          className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Loading..." : "Refresh"}
        </button>
      </div>

      {view.counts ? (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-ink-100/55">
          <span className="text-ink-100/40">Tiers:</span>
          <span title="Probationary lane — fast decay, gets promoted on use">
            scratchpad <span className="text-ink-100/80">{view.counts.scratchpad}</span>
          </span>
          <span className="text-ink-100/30">·</span>
          <span title="Verified anchors — normal decay">
            long_term <span className="text-ink-100/80">{view.counts.long_term}</span>
          </span>
          <span className="text-ink-100/30">·</span>
          <span title="Cold history — zero decay, needs a strong match to surface">
            archive <span className="text-ink-100/80">{view.counts.archive}</span>
          </span>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
          <span>Kind:</span>
          <select
            value={view.kindFilter ?? ""}
            onChange={(e) => onSetKindFilter(e.target.value || null)}
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            <option value="">all kinds</option>
            {MEMORY_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
          <span>Tier:</span>
          <select
            value={view.tierFilter ?? ""}
            onChange={(e) =>
              onSetTierFilter((e.target.value || null) as MemoryTier | null)
            }
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            <option value="">all tiers</option>
            {MEMORY_TIERS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
          <span>Sort:</span>
          <select
            value={view.order}
            onChange={(e) => onSetOrder(e.target.value as MemoryOrder)}
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            <option value="recent">recent first</option>
            <option value="top">top salience</option>
          </select>
        </label>
        <label
          className="flex items-center gap-1 text-[11px] text-ink-100/60"
          title="Schema v9 confidence band. Pure client-side filter on the current page; doesn't change the per-tier counts above."
        >
          <span>Confidence:</span>
          <select
            value={confidenceBand}
            onChange={(e) => setConfidenceBand(e.target.value as ConfidenceBand)}
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            {CONFIDENCE_BANDS.map((b) => (
              <option key={b.id} value={b.id}>
                {b.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => setNewOpen(!newOpen)}
          className="ml-auto rounded border border-white/10 px-2 py-1 text-[11px] text-ink-100/70 hover:border-emerald-400/60 hover:text-emerald-100"
        >
          {newOpen ? "Cancel" : "+ Add memory"}
        </button>
      </div>

      {error ? (
        <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      ) : null}

      {newOpen ? (
        <div className="space-y-2 rounded-md border border-emerald-400/30 bg-emerald-500/5 p-3">
          <textarea
            value={newDraft.content}
            onChange={(e) =>
              setNewDraft({ ...newDraft, content: e.target.value })
            }
            placeholder="What should Aiko remember?"
            rows={3}
            className="w-full resize-y rounded border border-white/10 bg-black/30 px-2 py-1.5 text-xs text-ink-100 placeholder-ink-100/30 focus:border-ink-400 focus:outline-none"
          />
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>kind:</span>
              <select
                value={newDraft.kind}
                onChange={(e) =>
                  setNewDraft({ ...newDraft, kind: e.target.value })
                }
                className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              >
                {MEMORY_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>salience {Math.round(newDraft.salience * 100)}%:</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={newDraft.salience}
                onChange={(e) =>
                  setNewDraft({ ...newDraft, salience: Number(e.target.value) })
                }
              />
            </label>
            <button
              type="button"
              onClick={onCreate}
              disabled={busy || newDraft.content.trim().length < 4}
              className="ml-auto rounded border border-emerald-400/40 bg-emerald-500/10 px-3 py-1 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save
            </button>
          </div>
        </div>
      ) : null}

      {visibleItems.length === 0 ? (
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          {confidenceBand !== "all" && view.items.length > 0
            ? `No memories on this page match the "${confidenceBand}" confidence filter.`
            : view.kindFilter
            ? `No memories with kind "${view.kindFilter}".`
            : "Nothing remembered yet. Memories are mined after a few turns of conversation, or whenever Aiko writes a private [[remember]] tag."}
        </p>
      ) : (
        <ul className="space-y-1.5">
          {visibleItems.map((memory) => {
            const isEditing = editingId === memory.id;
            return (
              <li
                key={memory.id}
                className={`rounded-md border px-3 py-2 text-xs ${
                  memory.pinned
                    ? "border-amber-400/40 bg-amber-500/5"
                    : "border-white/5 bg-white/[0.03]"
                }`}
              >
                {isEditing ? (
                  <div className="space-y-2">
                    <textarea
                      value={draft.content}
                      onChange={(e) =>
                        setDraft({ ...draft, content: e.target.value })
                      }
                      rows={3}
                      className="w-full resize-y rounded border border-white/10 bg-black/30 px-2 py-1.5 text-xs text-ink-100 focus:border-ink-400 focus:outline-none"
                    />
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                        <span>kind:</span>
                        <select
                          value={draft.kind}
                          onChange={(e) =>
                            setDraft({ ...draft, kind: e.target.value })
                          }
                          className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                        >
                          {MEMORY_KINDS.map((k) => (
                            <option key={k} value={k}>
                              {k}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                        <span>salience {Math.round(draft.salience * 100)}%:</span>
                        <input
                          type="range"
                          min={0}
                          max={1}
                          step={0.05}
                          value={draft.salience}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              salience: Number(e.target.value),
                            })
                          }
                        />
                      </label>
                      <div className="ml-auto flex gap-1">
                        <button
                          type="button"
                          onClick={onCancelEdit}
                          className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-white/30 hover:text-ink-100"
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={() => onSaveEdit(memory)}
                          disabled={busy || draft.content.trim().length < 4}
                          className="rounded border border-ink-400/40 bg-ink-500/20 px-2 py-0.5 text-[11px] text-ink-100 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          Save
                        </button>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      {(() => {
                        // Reflections written by the dream / knowledge-map
                        // workers carry a ``[dream] `` / ``[mindmap] ``
                        // content prefix that is a functional discriminator,
                        // not user-facing text. Strip it for display and show
                        // a small badge instead so the raw tag never leaks.
                        const { text, badge } = stripJournalPrefix(memory.content);
                        return (
                          <p className="break-words text-ink-100/90">
                            {badge ? (
                              <span
                                className="mr-1.5 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-violet-200"
                                title={
                                  badge === "dream"
                                    ? "A between-sessions dream (DreamWorker)."
                                    : "A noticing about the shape of what she knows (knowledge-map reflection)."
                                }
                              >
                                {badge}
                              </span>
                            ) : null}
                            {text}
                          </p>
                        );
                      })()}
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
                        <span className="rounded bg-white/5 px-1.5 py-0.5 text-ink-100/60">
                          {memory.kind}
                        </span>
                        {memory.tier ? (
                          <span
                            className={
                              memory.tier === "scratchpad"
                                ? "rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-200"
                                : memory.tier === "archive"
                                ? "rounded bg-slate-500/20 px-1.5 py-0.5 text-slate-200"
                                : "rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-200"
                            }
                            title={
                              memory.tier === "scratchpad"
                                ? "Probationary — promoted to long_term on use or revival, deleted after TTL"
                                : memory.tier === "archive"
                                ? "Cold history — zero decay, only surfaces on strong matches"
                                : "Verified anchor — normal decay"
                            }
                          >
                            {memory.tier}
                          </span>
                        ) : null}
                        <span>
                          salience {(memory.salience * 100).toFixed(0)}%
                        </span>
                        <ConfidencePip
                          confidence={memory.confidence}
                          conflicted={memoryIsConflicted(memory)}
                          verifiedAt={memoryVerifiedAt(memory)}
                        />
                        {typeof memory.revival_score === "number" && memory.revival_score > 0.05 ? (
                          <span
                            className="text-fuchsia-300/80"
                            title="Revival score: how often Aiko cites this memory in her replies. Drives a small salience rebate on every decay tick."
                          >
                            revival {(memory.revival_score * 100).toFixed(0)}%
                          </span>
                        ) : null}
                        {memory.use_count > 0 ? (
                          <span>used {memory.use_count}x</span>
                        ) : null}
                        {memory.pinned ? (
                          <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-amber-200">
                            pinned
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-col gap-1">
                      <button
                        type="button"
                        onClick={() => onPin(memory, !memory.pinned)}
                        className={`rounded border px-2 py-0.5 text-[11px] ${
                          memory.pinned
                            ? "border-amber-400/60 text-amber-200 hover:bg-amber-500/10"
                            : "border-white/10 text-ink-100/60 hover:border-amber-400/60 hover:text-amber-200"
                        }`}
                        aria-label={memory.pinned ? "Unpin memory" : "Pin memory"}
                      >
                        {memory.pinned ? "unpin" : "pin"}
                      </button>
                      <button
                        type="button"
                        onClick={() => onStartEdit(memory)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
                      >
                        edit
                      </button>
                      <button
                        type="button"
                        onClick={() => onDelete(memory)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                        aria-label={`Forget memory ${memory.id}`}
                      >
                        forget
                      </button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {pageCount > 1 ? (
        <div className="flex items-center justify-center gap-3 pt-1 text-[11px] text-ink-100/60">
          <button
            type="button"
            onClick={() => onSetPage(view.page - 1)}
            disabled={busy || view.page <= 0}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Prev
          </button>
          <span className="font-mono text-ink-100/40">
            page {view.page + 1} of {pageCount}
          </span>
          <button
            type="button"
            onClick={() => onSetPage(view.page + 1)}
            disabled={busy || view.page + 1 >= pageCount}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
          </button>
        </div>
      ) : null}
        </div>
      ) : null}

      {subTab === "gaps" ? <KnowledgeGapsPanel /> : null}

      {subTab === "conflicts" ? <MemoryConflictsPanel /> : null}

      {subTab === "beliefs" ? <BeliefsPanel /> : null}

      {subTab === "curiosity" ? <CuriositySeedsPanel /> : null}

      {subTab === "topics" ? <TopicGraphPanel /> : null}

      {subTab === "goals" ? <GoalsPanel /> : null}

      <FactCheckerStatusFooter />
    </Section>
  );
}
