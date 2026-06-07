/**
 * TasksTab — paginated history viewer for the background task
 * orchestrator (chunk 14).
 *
 * The strip above the chat shows what's happening *right now*; this
 * tab is the "where did that task go?" surface — full server-side
 * pagination over the entire history of visible-to-user tasks, with
 * a status filter pill bar. Cancellation and answering work the same
 * way as in the strip (REST → orchestrator → broadcast → store).
 *
 * Props-only contract: the parent SettingsDrawer owns REST loads
 * (so a route change can trigger a re-fetch) and passes the
 * resulting slice through. The component itself only renders + emits
 * intents.
 */
import { useState } from "react";

import { api } from "../../api";
import type { TaskEvent, TaskSnapshot, TaskStatus } from "../../types";
import {
  ACTIVE_TASK_STATUSES,
  TERMINAL_TASK_STATUSES,
} from "../../types";
import { Section, formatRelative } from "./SettingsSection";

export interface TasksTabProps {
  tasks: TaskSnapshot[];
  total: number;
  page: number;
  pageSize: number;
  statusFilter: TaskStatus | null;
  loading: boolean;
  enabled: boolean;
  error: string | null;
  onSetStatusFilter: (status: TaskStatus | null) => void;
  onSetPage: (page: number) => void;
  onCancel: (taskId: number) => void;
  onAnswer: (taskId: number, answer: string) => void;
  onRefresh: () => void;
}

const STATUS_FILTERS: ReadonlyArray<{
  id: TaskStatus | "all";
  label: string;
}> = [
  { id: "all", label: "all" },
  { id: "running", label: "running" },
  { id: "awaiting_input", label: "awaiting input" },
  { id: "done", label: "done" },
  { id: "failed", label: "failed" },
  { id: "cancelled", label: "cancelled" },
  { id: "interrupted", label: "interrupted" },
];

const STATUS_BADGE_CLASS: Record<TaskStatus, string> = {
  running: "bg-violet-500/15 text-violet-200",
  awaiting_input: "bg-amber-500/15 text-amber-200",
  paused: "bg-slate-500/15 text-slate-200",
  done: "bg-emerald-500/15 text-emerald-200",
  failed: "bg-rose-500/15 text-rose-200",
  cancelled: "bg-zinc-500/15 text-zinc-200",
  interrupted: "bg-yellow-700/15 text-yellow-200",
};

function statusLabel(status: TaskStatus): string {
  if (status === "awaiting_input") return "awaiting input";
  return status;
}

function isActive(status: TaskStatus): boolean {
  return ACTIVE_TASK_STATUSES.has(status);
}

function isTerminal(status: TaskStatus): boolean {
  return TERMINAL_TASK_STATUSES.has(status);
}

function PrimaryDetail({ task }: { task: TaskSnapshot }) {
  // Pick the one most informative line for the row. Priority:
  //   error (when failed) → result.summary (when done) →
  //   input_request.prompt (when awaiting input) → last_message →
  //   handler_name (always present, lowest priority)
  if (task.status === "failed" && task.error) {
    return (
      <span className="truncate text-xs text-rose-200/80">{task.error}</span>
    );
  }
  if (task.status === "done" && task.result) {
    const summary =
      typeof task.result.summary === "string" ? task.result.summary : "";
    if (summary) {
      return (
        <span className="truncate text-xs text-ink-100/60">{summary}</span>
      );
    }
  }
  if (task.status === "awaiting_input" && task.input_request?.prompt) {
    return (
      <span className="truncate text-xs italic text-ink-100/70">
        {task.input_request.prompt}
      </span>
    );
  }
  if (task.last_message) {
    return (
      <span className="truncate text-xs text-ink-100/60">
        {task.last_message}
      </span>
    );
  }
  return <span className="text-xs text-ink-100/40">{task.handler_name}</span>;
}

/** Schema v17: lazy-loaded per-task event timeline. Click "events"
 * to fetch; render is a flat chronological list. Cheap when never
 * expanded — the API call only fires on toggle.
 */
function EventsExpander({ taskId }: { taskId: number }) {
  const [open, setOpen] = useState(false);
  const [events, setEvents] = useState<TaskEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleToggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (events !== null || loading) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await api.listTaskEvents(taskId, { order: "asc", limit: 200 });
      setEvents(resp.events ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load events");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-2 flex flex-col gap-1">
      <button
        type="button"
        onClick={() => void handleToggle()}
        className="self-start rounded-md border border-white/10 bg-white/[0.02] px-2 py-0.5 text-[11px] text-ink-100/55 hover:bg-white/5"
        aria-expanded={open}
      >
        {open ? "hide events" : "events"}
      </button>
      {open ? (
        <div className="rounded-md border border-white/5 bg-white/[0.015] px-2 py-1.5">
          {loading ? (
            <div className="text-[11px] text-ink-100/45">loading…</div>
          ) : error ? (
            <div className="text-[11px] text-rose-300/80">{error}</div>
          ) : events === null || events.length === 0 ? (
            <div className="text-[11px] text-ink-100/45">no events</div>
          ) : (
            <ol className="flex flex-col gap-0.5 text-[11px] text-ink-100/60">
              {events.map((evt) => (
                <li key={evt.id} className="flex items-baseline gap-2">
                  <span className="shrink-0 font-mono text-ink-100/40">
                    {evt.created_at.slice(11, 19)}
                  </span>
                  <span className="shrink-0 rounded bg-white/5 px-1 py-0.5 text-[10px] uppercase tracking-wide">
                    {evt.type}
                  </span>
                  {evt.data ? (
                    <span className="truncate font-mono text-ink-100/55">
                      {JSON.stringify(evt.data)}
                    </span>
                  ) : null}
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </div>
  );
}

function TaskRow({
  task,
  onCancel,
  onAnswer,
}: {
  task: TaskSnapshot;
  onCancel: (taskId: number) => void;
  onAnswer: (taskId: number, answer: string) => void;
}) {
  const [freeText, setFreeText] = useState("");
  const options = task.input_request?.options || [];
  const hasOptions = task.status === "awaiting_input" && options.length > 0;
  const acceptsFreeText = task.status === "awaiting_input" && !hasOptions;
  const progressPct =
    typeof task.progress === "number"
      ? Math.round(Math.max(0, Math.min(1, task.progress)) * 100)
      : null;

  return (
    <li className="rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex items-center gap-2">
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                STATUS_BADGE_CLASS[task.status] ?? "bg-white/5 text-ink-100/70"
              }`}
            >
              {statusLabel(task.status)}
            </span>
            <span className="truncate text-sm font-medium text-ink-100">
              {task.title || task.handler_name}
            </span>
            {task.phase && task.status === "running" ? (
              <span className="rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-violet-200">
                {task.phase}
              </span>
            ) : null}
            {progressPct !== null && task.status === "running" ? (
              <span className="text-xs text-ink-100/45">{progressPct}%</span>
            ) : null}
          </div>
          <PrimaryDetail task={task} />
          <div className="flex flex-wrap items-center gap-3 text-[11px] text-ink-100/40">
            <span>handler: {task.handler_name}</span>
            <span>started {formatRelative(task.created_at)}</span>
            {isTerminal(task.status) && task.completed_at ? (
              <span>finished {formatRelative(task.completed_at)}</span>
            ) : null}
            <span>id #{task.id}</span>
            {task.parent_task_id ? (
              <span>parent #{task.parent_task_id}</span>
            ) : null}
          </div>
        </div>
        {isActive(task.status) ? (
          <button
            type="button"
            onClick={() => onCancel(task.id)}
            className="shrink-0 rounded-md border border-white/10 bg-transparent px-2 py-1 text-xs text-ink-100/55 hover:bg-white/5 hover:text-ink-100/80"
          >
            cancel
          </button>
        ) : null}
      </div>
      {task.status === "awaiting_input" ? (
        <div className="mt-2 flex flex-col gap-1.5">
          {hasOptions ? (
            <div className="flex flex-wrap gap-1.5">
              {options.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => onAnswer(task.id, opt)}
                  className="rounded-full border border-amber-300/40 bg-amber-300/10 px-2.5 py-1 text-xs text-ink-100/85 hover:bg-amber-300/20"
                >
                  {opt}
                </button>
              ))}
            </div>
          ) : null}
          {acceptsFreeText ? (
            <form
              className="flex items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                const trimmed = freeText.trim();
                if (!trimmed) return;
                onAnswer(task.id, trimmed);
                setFreeText("");
              }}
            >
              <input
                type="text"
                value={freeText}
                onChange={(e) => setFreeText(e.target.value)}
                placeholder="answer…"
                className="min-w-0 flex-1 rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-ink-100/90 placeholder:text-ink-100/30 focus:border-amber-300/40 focus:outline-none"
              />
              <button
                type="submit"
                disabled={!freeText.trim()}
                className="rounded-md border border-amber-300/40 bg-amber-300/10 px-2.5 py-1 text-xs text-ink-100/85 hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-60"
              >
                send
              </button>
            </form>
          ) : null}
        </div>
      ) : null}
      <EventsExpander taskId={task.id} />
    </li>
  );
}

export function TasksTab({
  tasks,
  total,
  page,
  pageSize,
  statusFilter,
  loading,
  enabled,
  error,
  onSetStatusFilter,
  onSetPage,
  onCancel,
  onAnswer,
  onRefresh,
}: TasksTabProps) {
  const numPages = Math.max(1, Math.ceil(total / pageSize));
  const pageStart = total === 0 ? 0 : page * pageSize + 1;
  const pageEnd = Math.min(total, (page + 1) * pageSize);

  if (!enabled) {
    return (
      <Section title="Tasks">
        <div className="rounded-md border border-white/5 bg-white/[0.02] p-4 text-sm text-ink-100/60">
          Background tasks are disabled. Set
          <code className="mx-1 rounded bg-white/10 px-1 py-0.5 text-xs">
            agent.tasks_enabled
          </code>
          in <code>config/user.json</code> to surface this view.
        </div>
      </Section>
    );
  }

  return (
    <Section title="Tasks">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          {STATUS_FILTERS.map((filter) => {
            const active =
              filter.id === "all"
                ? statusFilter === null
                : statusFilter === filter.id;
            return (
              <button
                key={filter.id}
                type="button"
                onClick={() =>
                  onSetStatusFilter(filter.id === "all" ? null : filter.id)
                }
                className={`rounded-full border px-2.5 py-0.5 text-xs ${
                  active
                    ? "border-violet-400/50 bg-violet-400/15 text-ink-100/90"
                    : "border-white/10 bg-white/[0.02] text-ink-100/55 hover:bg-white/5"
                }`}
              >
                {filter.label}
              </button>
            );
          })}
          <div className="ml-auto flex items-center gap-2 text-xs text-ink-100/45">
            <button
              type="button"
              onClick={onRefresh}
              className="rounded-md border border-white/10 bg-white/[0.02] px-2 py-0.5 text-ink-100/60 hover:bg-white/5"
              disabled={loading}
            >
              {loading ? "loading…" : "refresh"}
            </button>
          </div>
        </div>

        {error ? (
          <div className="rounded-md border border-rose-400/30 bg-rose-400/10 px-3 py-2 text-xs text-rose-200/90">
            {error}
          </div>
        ) : null}

        {tasks.length === 0 ? (
          <div className="rounded-md border border-white/5 bg-white/[0.02] p-4 text-center text-sm text-ink-100/45">
            {loading
              ? "loading tasks…"
              : statusFilter
                ? `no ${statusFilter} tasks yet`
                : "no tasks yet — Aiko will queue background work here as it happens"}
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {tasks.map((task) => (
              <TaskRow
                key={task.id}
                task={task}
                onCancel={onCancel}
                onAnswer={onAnswer}
              />
            ))}
          </ul>
        )}

        {total > 0 ? (
          <div className="flex items-center justify-between text-xs text-ink-100/45">
            <span>
              showing {pageStart}–{pageEnd} of {total}
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={page === 0 || loading}
                onClick={() => onSetPage(Math.max(0, page - 1))}
                className="rounded-md border border-white/10 bg-white/[0.02] px-2 py-0.5 text-ink-100/60 hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50"
              >
                prev
              </button>
              <span>
                page {page + 1} / {numPages}
              </span>
              <button
                type="button"
                disabled={page + 1 >= numPages || loading}
                onClick={() => onSetPage(page + 1)}
                className="rounded-md border border-white/10 bg-white/[0.02] px-2 py-0.5 text-ink-100/60 hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50"
              >
                next
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </Section>
  );
}
