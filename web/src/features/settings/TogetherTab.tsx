import type { SharedMoment, TogetherSummary } from "../../types";
import { SHARED_MOMENT_VIBES } from "../../types";
import { Section } from "./SettingsSection";

export interface TogetherTabProps {
  summary: TogetherSummary | null;
  moments: SharedMoment[];
  total: number;
  page: number;
  pageSize: number;
  vibeFilter: string | null;
  loading: boolean;
  error: string | null;
  onSetVibeFilter: (vibe: string | null) => void;
  onSetPage: (page: number) => void;
  newOpen: boolean;
  setNewOpen: (open: boolean) => void;
  newDraft: { summary: string; vibe: string; when: string };
  setNewDraft: (
    draft: { summary: string; vibe: string; when: string },
  ) => void;
  onCreate: () => void;
  editingId: number | null;
  setEditingId: (id: number | null) => void;
  editDraft: { summary: string; vibe: string; when: string };
  setEditDraft: (
    draft: { summary: string; vibe: string; when: string },
  ) => void;
  onSaveEdit: () => void;
  onDelete: (moment: SharedMoment) => void;
  onTogglePin: (moment: SharedMoment) => void;
  onRefresh: () => void;
}

export function TogetherTab({
  summary,
  moments,
  total,
  page,
  pageSize,
  vibeFilter,
  loading,
  error,
  onSetVibeFilter,
  onSetPage,
  newOpen,
  setNewOpen,
  newDraft,
  setNewDraft,
  onCreate,
  editingId,
  setEditingId,
  editDraft,
  setEditDraft,
  onSaveEdit,
  onDelete,
  onTogglePin,
  onRefresh,
}: TogetherTabProps) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="space-y-4">
      {error ? (
        <div className="rounded-md border border-red-400/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-200">
          {error}
        </div>
      ) : null}

      {/* Header */}
      <Section title="The story so far">
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-ink-100/80">
          {summary ? (
            <>
              <span className="rounded-full border border-pink-400/30 bg-pink-500/10 px-2 py-0.5 text-[11px] uppercase tracking-wide text-pink-200">
                {summary.phase.replace(/_/g, " ")}
              </span>
              <span>·</span>
              <span>
                <b>{summary.days_known}</b> days known
              </span>
              <span>·</span>
              <span>
                <b>{summary.total_turns}</b> turns
              </span>
              <span>·</span>
              <span>
                <b>{summary.total_sessions}</b> sessions
              </span>
            </>
          ) : (
            <span className="text-ink-100/40">{loading ? "Loading…" : "—"}</span>
          )}
          <button
            type="button"
            onClick={onRefresh}
            className="ml-auto rounded-md border border-white/10 px-2 py-1 text-[11px] hover:bg-white/[0.04]"
          >
            Refresh
          </button>
        </div>
      </Section>

      {/* Anniversary card */}
      {summary?.anniversary_today ? (
        <div className="rounded-md border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-100">
          <div className="text-[10px] uppercase tracking-wider text-amber-200/80">
            On your mind today
          </div>
          <div className="mt-1 text-amber-50">
            {summary.anniversary_today.window_label}:{" "}
            {summary.anniversary_today.summary}
          </div>
          <div className="mt-1 text-[10px] uppercase tracking-wide text-amber-200/60">
            vibe · {summary.anniversary_today.vibe}
          </div>
        </div>
      ) : null}

      {/* Axes bars */}
      {summary?.axes ? (
        <Section title="How the relationship feels">
          <div className="space-y-2">
            <AxisBar label="Closeness" value={summary.axes.closeness} />
            <AxisBar label="Humor" value={summary.axes.humor} />
            <AxisBar label="Trust" value={summary.axes.trust} />
            <AxisBar label="Comfort" value={summary.axes.comfort} />
          </div>
        </Section>
      ) : null}

      {/* Milestones */}
      {summary?.milestones?.length ? (
        <Section title="Milestones">
          <ul className="space-y-1 text-[12px]">
            {summary.milestones.map((m) => (
              <li
                key={m.label}
                className={`flex items-center gap-2 rounded-md px-2 py-1 ${
                  m.crossed ? "bg-emerald-500/10 text-emerald-100" : "text-ink-100/55"
                }`}
              >
                <span>{m.crossed ? "✓" : "·"}</span>
                <span>{m.human}</span>
                {m.crossed_at ? (
                  <span className="ml-auto font-mono text-[10px] text-ink-100/40">
                    {new Date(m.crossed_at).toLocaleDateString()}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {/* Moments timeline */}
      <Section title={`Shared moments (${total})`}>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-ink-100/60">
          <label className="flex items-center gap-1">
            Filter
            <select
              value={vibeFilter ?? ""}
              onChange={(e) =>
                onSetVibeFilter(e.target.value ? e.target.value : null)
              }
              className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
            >
              <option value="">all vibes</option>
              {SHARED_MOMENT_VIBES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => setNewOpen(!newOpen)}
            className="ml-auto rounded-md border border-white/10 px-2 py-1 hover:bg-white/[0.04]"
          >
            {newOpen ? "Cancel" : "+ Add manually"}
          </button>
        </div>

        {newOpen ? (
          <div className="rounded-md border border-white/10 bg-white/[0.03] p-3">
            <div className="space-y-2">
              <label className="block text-[11px] text-ink-100/55">
                Summary
                <textarea
                  value={newDraft.summary}
                  onChange={(e) =>
                    setNewDraft({ ...newDraft, summary: e.target.value })
                  }
                  rows={2}
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[12px]"
                />
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="block text-[11px] text-ink-100/55">
                  Vibe
                  <select
                    value={newDraft.vibe}
                    onChange={(e) =>
                      setNewDraft({ ...newDraft, vibe: e.target.value })
                    }
                    className="mt-1 w-full rounded-md border border-white/10 bg-black/30 px-2 py-1"
                  >
                    {SHARED_MOMENT_VIBES.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-[11px] text-ink-100/55">
                  When (ISO, optional)
                  <input
                    value={newDraft.when}
                    onChange={(e) =>
                      setNewDraft({ ...newDraft, when: e.target.value })
                    }
                    placeholder="2025-04-15T14:00:00Z"
                    className="mt-1 w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
                  />
                </label>
              </div>
              <button
                type="button"
                onClick={onCreate}
                disabled={newDraft.summary.trim().length < 4}
                className="rounded-md bg-pink-500/30 px-3 py-1 text-[12px] hover:bg-pink-500/40 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Save moment
              </button>
            </div>
          </div>
        ) : null}

        {loading && moments.length === 0 ? (
          <div className="text-[11px] text-ink-100/40">Loading…</div>
        ) : moments.length === 0 ? (
          <div className="text-[11px] text-ink-100/40">No moments yet.</div>
        ) : (
          <ul className="space-y-2">
            {moments.map((moment) => (
              <MomentCard
                key={moment.id}
                moment={moment}
                editing={editingId === moment.id}
                draft={editDraft}
                setDraft={setEditDraft}
                onStartEdit={() => {
                  setEditingId(moment.id);
                  setEditDraft({
                    summary: moment.summary,
                    vibe: String(moment.vibe),
                    when: moment.when,
                  });
                }}
                onCancelEdit={() => setEditingId(null)}
                onSaveEdit={onSaveEdit}
                onDelete={() => onDelete(moment)}
                onTogglePin={() => onTogglePin(moment)}
              />
            ))}
          </ul>
        )}

        {pageCount > 1 ? (
          <div className="flex items-center justify-between text-[11px] text-ink-100/55">
            <button
              type="button"
              disabled={page <= 0}
              onClick={() => onSetPage(Math.max(0, page - 1))}
              className="rounded-md border border-white/10 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
            >
              ← Prev
            </button>
            <span>
              page {page + 1} / {pageCount}
            </span>
            <button
              type="button"
              disabled={page >= pageCount - 1}
              onClick={() => onSetPage(Math.min(pageCount - 1, page + 1))}
              className="rounded-md border border-white/10 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next →
            </button>
          </div>
        ) : null}
      </Section>
    </div>
  );
}

interface MomentCardProps {
  moment: SharedMoment;
  editing: boolean;
  draft: { summary: string; vibe: string; when: string };
  setDraft: (
    draft: { summary: string; vibe: string; when: string },
  ) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
  onDelete: () => void;
  onTogglePin: () => void;
}

function MomentCard({
  moment,
  editing,
  draft,
  setDraft,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onDelete,
  onTogglePin,
}: MomentCardProps) {
  const date = (() => {
    try {
      return new Date(moment.when).toLocaleDateString();
    } catch {
      return moment.when;
    }
  })();
  return (
    <li className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/55">
        <span className="rounded-full bg-white/[0.04] px-2 py-0.5 font-mono">
          {date}
        </span>
        <span className="rounded-full border border-white/10 px-2 py-0.5">
          {moment.vibe}
        </span>
        <span className="text-ink-100/40">via {moment.source}</span>
        {moment.pinned ? (
          <span
            className="ml-1 text-amber-200"
            title="Pinned — never decays"
          >
            ★
          </span>
        ) : null}
        <div className="ml-auto flex gap-1">
          {editing ? (
            <>
              <button
                type="button"
                onClick={onSaveEdit}
                className="rounded-md bg-pink-500/30 px-2 py-0.5 text-[10px] hover:bg-pink-500/40"
              >
                Save
              </button>
              <button
                type="button"
                onClick={onCancelEdit}
                className="rounded-md border border-white/10 px-2 py-0.5 text-[10px] hover:bg-white/[0.04]"
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={onStartEdit}
                className="rounded-md border border-white/10 px-2 py-0.5 text-[10px] hover:bg-white/[0.04]"
              >
                Edit
              </button>
              <button
                type="button"
                onClick={onTogglePin}
                className="rounded-md border border-white/10 px-2 py-0.5 text-[10px] hover:bg-white/[0.04]"
              >
                {moment.pinned ? "Unpin" : "Pin"}
              </button>
              <button
                type="button"
                onClick={onDelete}
                className="rounded-md border border-red-400/30 px-2 py-0.5 text-[10px] text-red-200 hover:bg-red-500/10"
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <div className="mt-2 space-y-2">
          <textarea
            value={draft.summary}
            onChange={(e) => setDraft({ ...draft, summary: e.target.value })}
            rows={2}
            className="w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[12px]"
          />
          <div className="grid grid-cols-2 gap-2">
            <select
              value={draft.vibe}
              onChange={(e) => setDraft({ ...draft, vibe: e.target.value })}
              className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
            >
              {SHARED_MOMENT_VIBES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            <input
              value={draft.when}
              onChange={(e) => setDraft({ ...draft, when: e.target.value })}
              placeholder="ISO datetime"
              className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
            />
          </div>
        </div>
      ) : (
        <div className="mt-1 text-ink-100/85">{moment.summary}</div>
      )}
    </li>
  );
}

function AxisBar({
  label,
  value,
}: {
  label: string;
  value: number;
}) {
  // Map [-1, 1] to [0, 100]% for the bar. Centre line at 50%.
  const clamped = Math.max(-1, Math.min(1, Number(value) || 0));
  const isPositive = clamped >= 0;
  const halfWidth = Math.abs(clamped) * 50;
  const color = isPositive ? "bg-emerald-400/70" : "bg-rose-400/70";
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-[11px]">
        <span className="text-ink-100/65">{label}</span>
        <span className="font-mono text-ink-100/55">
          {clamped >= 0 ? "+" : ""}
          {clamped.toFixed(2)}
        </span>
      </div>
      <div className="relative h-2 overflow-hidden rounded-full bg-white/[0.05]">
        {/* centre line */}
        <div className="absolute left-1/2 top-0 h-full w-px bg-white/15" />
        {/* value bar */}
        <div
          className={`absolute top-0 h-full ${color}`}
          style={{
            width: `${halfWidth}%`,
            left: isPositive ? "50%" : `${50 - halfWidth}%`,
          }}
        />
      </div>
    </div>
  );
}
