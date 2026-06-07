/**
 * PersonaTaskBanner — chunk 15 mirror of ``TaskStrip`` for the
 * detached persona window (``index.html#/persona``).
 *
 * The persona overlay never renders the chat strip, so an
 * ``awaiting_input`` task — the only case where a click would be
 * meaningfully faster than typing — has no UI surface there
 * without this component. The chat-first answer path (Aiko asks
 * naturally in her next turn, user replies in chat, LLM emits
 * ``answer_task``) keeps working through voice + persona-window
 * input regardless of whether this banner is rendered. The
 * banner is a *click fallback*, not the primary flow — same
 * posture as ``TaskStrip`` and matching what the design doc
 * (``docs/brain-orchestration.md`` § "Channel B — UI click") calls
 * out.
 *
 * Behaviour:
 *
 *   1. **Trigger** — subscribes to the active task slice. The
 *      banner picks the most-recent ``awaiting_input`` task from
 *      ``activeIds`` (newest-first, same ordering as the strip).
 *      When that task transitions away from ``awaiting_input``
 *      (answered, cancelled, or completed) the banner auto-hides;
 *      if a *different* task is still waiting it takes over.
 *
 *   2. **Render** — pill near the avatar with the task title +
 *      the prompt and either clickable option buttons (when
 *      ``input_request.options`` is non-empty) or a tiny inline
 *      text field. A "cancel" affordance cancels the task on the
 *      server; a separate "✕" dismisses the banner without
 *      cancelling, so the chat-channel answer path stays open.
 *
 *   3. **Dismiss tracking** — dismissed task ids are stashed in
 *      local component state so the same banner doesn't bounce
 *      back as soon as the next render reads the same task.
 *      A different task id is still surfaced.
 *
 *   4. **Master switch** — guards on
 *      ``agent.persona_task_banner_enabled`` (threaded by
 *      ``PersonaWindow``). When ``false`` the component is a
 *      no-op and returns ``null``.
 *
 * Layout note: the banner sits BELOW the K31 touch banner so a
 * gesture pill and a task pill never overlap on the avatar.
 */
import { useCallback, useMemo, useState } from "react";

import { api } from "../api";
import { useAssistantStore } from "../store";
import type { TaskSnapshot } from "../types";

export interface PersonaTaskBannerProps {
  /** Master switch. Mirrors the server-side
   * ``agent.persona_task_banner_enabled`` setting. ``PersonaWindow``
   * threads it in; defaults to ``true`` so callers that don't yet
   * have a settings snapshot still see the feature. */
  enabled?: boolean;
}

/** Pick the awaiting_input task to surface. Newest-first (the
 * ``activeIds`` order already matches that) and skips ids that
 * the user has dismissed in this session. Pure function so the
 * component test can call it directly. */
export function pickAwaitingTask(
  activeIds: ReadonlyArray<number>,
  tasksById: Readonly<Record<number, TaskSnapshot>>,
  dismissed: ReadonlySet<number>,
): TaskSnapshot | null {
  for (const id of activeIds) {
    if (dismissed.has(id)) continue;
    const row = tasksById[id];
    if (!row) continue;
    if (row.status === "awaiting_input") return row;
  }
  return null;
}

export function PersonaTaskBanner({
  enabled = true,
}: PersonaTaskBannerProps) {
  const tasksById = useAssistantStore((s) => s.tasksView.tasksById);
  const activeIds = useAssistantStore((s) => s.tasksView.activeIds);
  const pushToast = useAssistantStore((s) => s.pushToast);

  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const [freeText, setFreeText] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const task = useMemo<TaskSnapshot | null>(
    () => pickAwaitingTask(activeIds, tasksById, dismissed),
    [activeIds, tasksById, dismissed],
  );

  const options = task?.input_request?.options || [];
  const hasOptions = options.length > 0;

  const handleOptionClick = useCallback(
    async (option: string) => {
      if (!task || submitting) return;
      setSubmitting(true);
      try {
        await api.answerTask(task.id, option);
        // Backend listener will fire ``task_progress`` /
        // ``task_completed`` over WS so the strip + Tasks tab
        // update without us mutating the store optimistically
        // here. The banner self-hides as soon as the task leaves
        // ``awaiting_input``.
      } catch (err) {
        pushToast("error", `Couldn't send answer: ${String(err)}`);
      } finally {
        setSubmitting(false);
      }
    },
    [task, submitting, pushToast],
  );

  const handleFreeTextSubmit = useCallback(async () => {
    if (!task || submitting) return;
    const trimmed = freeText.trim();
    if (!trimmed) return;
    setSubmitting(true);
    try {
      await api.answerTask(task.id, trimmed);
      setFreeText("");
    } catch (err) {
      pushToast("error", `Couldn't send answer: ${String(err)}`);
    } finally {
      setSubmitting(false);
    }
  }, [task, freeText, submitting, pushToast]);

  const handleCancelClick = useCallback(async () => {
    if (!task || submitting) return;
    setSubmitting(true);
    try {
      await api.cancelTask(task.id);
    } catch (err) {
      pushToast("error", `Couldn't cancel task: ${String(err)}`);
    } finally {
      setSubmitting(false);
    }
  }, [task, submitting, pushToast]);

  const handleDismissClick = useCallback(() => {
    if (!task) return;
    // Stash the id so the next render doesn't immediately
    // re-surface the same banner. The underlying task continues
    // to wait — the user can answer in chat / via voice.
    setDismissed((prev) => {
      if (prev.has(task.id)) return prev;
      const next = new Set(prev);
      next.add(task.id);
      return next;
    });
  }, [task]);

  if (!enabled || !task) return null;

  const prompt = task.input_request?.prompt || "Aiko needs your input.";
  const title = task.title || task.handler_name;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="persona-task-banner"
      data-task-id={task.id}
      className="pointer-events-auto absolute inset-x-2 top-24 z-30 mx-auto flex max-w-md flex-col gap-1.5 rounded-xl border border-amber-300/40 bg-black/75 px-3 py-2 text-sm text-amber-50 shadow-xl backdrop-blur"
    >
      <div className="flex items-center gap-2">
        <span aria-hidden="true" className="text-base leading-none">
          ❓
        </span>
        <span className="flex-1 truncate font-medium" title={title}>
          {title}
        </span>
        <button
          type="button"
          onClick={() => void handleCancelClick()}
          disabled={submitting}
          className="rounded-md border border-white/10 bg-transparent px-1.5 py-0.5 text-xs text-ink-100/55 hover:bg-white/10 hover:text-ink-100/80 disabled:cursor-not-allowed disabled:opacity-60"
          aria-label="Cancel task"
        >
          cancel
        </button>
        <button
          type="button"
          onClick={handleDismissClick}
          aria-label="Dismiss banner"
          className="flex h-5 w-5 items-center justify-center rounded text-ink-100/50 hover:bg-white/10 hover:text-ink-100"
        >
          ×
        </button>
      </div>
      <div className="text-xs italic text-ink-100/80">{prompt}</div>
      {hasOptions ? (
        <div className="flex flex-wrap gap-1.5">
          {options.map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => void handleOptionClick(opt)}
              disabled={submitting}
              className="rounded-full border border-amber-300/40 bg-amber-300/10 px-2.5 py-1 text-xs text-amber-50 hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {opt}
            </button>
          ))}
        </div>
      ) : (
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
            disabled={submitting}
            className="min-w-0 flex-1 rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-amber-50 placeholder:text-ink-100/30 focus:border-amber-300/40 focus:outline-none disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={!freeText.trim() || submitting}
            className="rounded-md border border-amber-300/40 bg-amber-300/10 px-2.5 py-1 text-xs text-amber-50 hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-60"
          >
            send
          </button>
        </form>
      )}
    </div>
  );
}
