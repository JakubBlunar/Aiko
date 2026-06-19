/**
 * TaskStrip — compact live view of background tasks (chunk 14).
 *
 * The canonical "right now" surface for the brain-orchestration task
 * system. One chip per active task plus a fade-out grace window for
 * each recently-completed task. The strip mounts at the top of the
 * chat column above the message list, mirrors the always-visible
 * ``ToolActivityStrip`` pattern but with more affordances:
 *
 *  - Running task → animated progress bar + last_message + cancel
 *  - awaiting_input → prompt + clickable option buttons (channel B
 *    in ``docs/brain-orchestration.md``). Free-text questions also
 *    surface a tiny inline text field.
 *  - done / failed / cancelled → final status line + dismiss
 *
 * The strip exclusively reads / writes the ``tasksView`` slice. WS
 * events arrive via ``useAssistantSocket``; the REST mutators
 * (``cancelTask`` / ``answerTask``) call the orchestrator directly
 * — the orchestrator then fires the matching listener event and the
 * store updates again on the round-trip. No optimistic state.
 *
 * Chip lifecycle uses an interval-driven sweep (1 Hz) instead of a
 * per-chip timer so a dozen completed tasks don't spawn a dozen
 * setTimeouts. ``TASK_STRIP_FADE_MS`` is the grace window terminal
 * chips stay visible.
 */
import { useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { useAssistantStore } from "../store";
import type { TaskSnapshot, TaskStatus } from "../types";

/** How long a terminal chip stays visible after its ``completed_at``
 * before the sweep fades it. The grace window is generous enough
 * that a user looking away from the screen can still glance back
 * and see "done", but short enough that the strip doesn't grow
 * unbounded across a long session. */
export const TASK_STRIP_FADE_MS = 20_000;

/** Sweep cadence. 1 Hz keeps it cheap and is plenty for a fade
 * window measured in seconds. */
const SWEEP_INTERVAL_MS = 1_000;

export interface TaskChipProps {
  task: TaskSnapshot;
  onCancel: (taskId: number) => void;
  onAnswer: (taskId: number, answer: string) => void;
  onDismiss: (taskId: number) => void;
}

const STATUS_ICON: Record<TaskStatus, string> = {
  running: "⚙️",
  awaiting_input: "❓",
  paused: "⏸",
  done: "✅",
  failed: "⚠️",
  cancelled: "🚫",
  interrupted: "🔌",
};

const STATUS_LABEL: Record<TaskStatus, string> = {
  running: "running",
  awaiting_input: "waiting on you",
  paused: "paused",
  done: "done",
  failed: "failed",
  cancelled: "cancelled",
  interrupted: "interrupted",
};

function isTerminal(status: TaskStatus): boolean {
  return (
    status === "done" ||
    status === "failed" ||
    status === "cancelled" ||
    status === "interrupted"
  );
}

function formatProgress(progress: number | null): string {
  if (progress === null || Number.isNaN(progress)) return "";
  const pct = Math.round(Math.max(0, Math.min(1, progress)) * 100);
  return `${pct}%`;
}

export function TaskChip({
  task,
  onCancel,
  onAnswer,
  onDismiss,
}: TaskChipProps) {
  const [freeText, setFreeText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const icon = STATUS_ICON[task.status] ?? "•";
  const statusLabel = STATUS_LABEL[task.status] ?? task.status;
  const terminal = isTerminal(task.status);
  const options = task.input_request?.options || [];
  const hasOptions = task.status === "awaiting_input" && options.length > 0;
  const acceptsFreeText = task.status === "awaiting_input" && !hasOptions;
  const subtitle =
    task.status === "failed"
      ? task.error || task.last_message || "task failed"
      : task.status === "done" && task.result
        ? (typeof task.result.summary === "string"
            ? task.result.summary
            : "") || task.last_message || ""
        : task.last_message || "";
  const progressPct = formatProgress(task.progress);
  const ariaLabel = `${task.title} (${statusLabel})`;

  async function handleOptionClick(option: string) {
    if (submitting) return;
    setSubmitting(true);
    try {
      await api.answerTask(task.id, option);
      onAnswer(task.id, option);
    } catch {
      // The server-side answer call returned 4xx/5xx; surface a
      // status by clearing the lock so the user can retry.
    } finally {
      setSubmitting(false);
    }
  }

  async function handleFreeTextSubmit() {
    const trimmed = freeText.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      await api.answerTask(task.id, trimmed);
      setFreeText("");
      onAnswer(task.id, trimmed);
    } catch {
      /* leave the input populated so the user can retry */
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCancelClick() {
    if (submitting) return;
    setSubmitting(true);
    try {
      await api.cancelTask(task.id);
      onCancel(task.id);
    } catch {
      /* listener will eventually deliver the terminal state */
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <li
      className={`flex items-start gap-3 rounded-lg border px-3 py-2 ${
        terminal
          ? "border-white/5 bg-white/[0.015] text-ink-100/55"
          : task.status === "awaiting_input"
            ? "border-amber-300/30 bg-amber-300/[0.05] text-ink-100/80"
            : "border-white/10 bg-white/[0.03] text-ink-100/80"
      }`}
      aria-label={ariaLabel}
      data-task-id={task.id}
      data-task-status={task.status}
    >
      <span aria-hidden="true" className="text-base leading-none">
        {icon}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className="truncate text-sm font-medium text-ink-100">
            {task.title || task.handler_name}
          </span>
          <span className="shrink-0 text-xs uppercase tracking-wide text-ink-100/50">
            {/* Schema v17: phase (e.g. "scanning") rides next to the
                status label when the handler set one. Falls back to
                the bare status label so legacy handlers still read
                the same. */}
            {task.phase && task.status === "running"
              ? `${task.phase} · ${statusLabel}`
              : statusLabel}
            {progressPct && task.status === "running" ? ` · ${progressPct}` : ""}
          </span>
        </div>
        {task.status === "running" && task.progress !== null ? (
          <div
            className="h-1 w-full overflow-hidden rounded-full bg-white/5"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(
              Math.max(0, Math.min(1, task.progress)) * 100,
            )}
          >
            <div
              className="h-full bg-violet-400/70 transition-[width] duration-300"
              style={{
                width: `${Math.round(
                  Math.max(0, Math.min(1, task.progress)) * 100,
                )}%`,
              }}
            />
          </div>
        ) : null}
        {subtitle ? (
          <div className="truncate text-xs text-ink-100/60">{subtitle}</div>
        ) : null}
        {task.status === "awaiting_input" && task.input_request ? (
          <div className="mt-1 flex flex-col gap-1">
            <div className="text-xs italic text-ink-100/70">
              {task.input_request.prompt}
            </div>
            {hasOptions ? (
              <div className="flex flex-wrap gap-1.5">
                {options.map((opt) => (
                  <button
                    key={opt}
                    type="button"
                    disabled={submitting}
                    onClick={() => void handleOptionClick(opt)}
                    className="rounded-full border border-amber-300/40 bg-amber-300/10 px-2.5 py-1 text-xs text-ink-100/85 hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-60"
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
                  void handleFreeTextSubmit();
                }}
              >
                <input
                  type="text"
                  value={freeText}
                  onChange={(e) => setFreeText(e.target.value)}
                  placeholder="answer…"
                  className="min-w-0 flex-1 rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-ink-100/90 placeholder:text-ink-100/30 focus:border-amber-300/40 focus:outline-none"
                  disabled={submitting}
                />
                <button
                  type="submit"
                  disabled={!freeText.trim() || submitting}
                  className="rounded-md border border-amber-300/40 bg-amber-300/10 px-2.5 py-1 text-xs text-ink-100/85 hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  send
                </button>
              </form>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {(task.status === "running" || task.status === "awaiting_input") &&
        !submitting ? (
          <button
            type="button"
            onClick={() => void handleCancelClick()}
            className="rounded-md border border-white/10 bg-transparent px-2 py-0.5 text-xs text-ink-100/55 hover:bg-white/5 hover:text-ink-100/80"
            aria-label="Cancel task"
          >
            cancel
          </button>
        ) : null}
        {terminal ? (
          <button
            type="button"
            onClick={() => onDismiss(task.id)}
            className="rounded-full px-1.5 text-xs text-ink-100/35 hover:text-ink-100/70"
            aria-label="Dismiss task"
            title="Dismiss"
          >
            ✕
          </button>
        ) : null}
      </div>
    </li>
  );
}

export function TaskStrip() {
  const tasksById = useAssistantStore((s) => s.tasksView.tasksById);
  const activeIds = useAssistantStore((s) => s.tasksView.activeIds);
  const dismissTaskFromStrip = useAssistantStore(
    (s) => s.dismissTaskFromStrip,
  );
  const sweepRecentlyCompletedTasks = useAssistantStore(
    (s) => s.sweepRecentlyCompletedTasks,
  );

  // Project active ids into a render list, dropping unknown ids
  // (defensive — the WS reducers guarantee they're present, but a
  // future code path that calls ``dismissTaskFromStrip`` racing
  // with a fresh broadcast shouldn't crash the strip).
  const tasks = useMemo<TaskSnapshot[]>(
    () =>
      activeIds
        .map((id) => tasksById[id])
        .filter((t): t is TaskSnapshot => Boolean(t)),
    [activeIds, tasksById],
  );

  // 1 Hz sweep: drop terminal chips whose ``completed_at`` is older
  // than ``TASK_STRIP_FADE_MS``. The reducer is a cheap pure check
  // that bails out when nothing has aged past the threshold.
  useEffect(() => {
    if (activeIds.length === 0) return;
    const id = window.setInterval(() => {
      sweepRecentlyCompletedTasks(TASK_STRIP_FADE_MS);
    }, SWEEP_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [activeIds.length, sweepRecentlyCompletedTasks]);

  if (tasks.length === 0) return null;

  return (
    <div
      className="max-h-[40vh] shrink-0 overflow-y-auto border-b border-white/5 bg-white/[0.015] px-6 py-2"
      data-testid="task-strip"
    >
      <ul className="mx-auto flex min-w-0 max-w-3xl flex-col gap-1.5">
        {tasks.map((task) => (
          <TaskChip
            key={task.id}
            task={task}
            onCancel={(id) => dismissTaskFromStrip(id)}
            onAnswer={() => {
              // Server will fire ``task_started``→``task_completed`` via
              // the listener; nothing to do here optimistically.
            }}
            onDismiss={(id) => dismissTaskFromStrip(id)}
          />
        ))}
      </ul>
    </div>
  );
}
