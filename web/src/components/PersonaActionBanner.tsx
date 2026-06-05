import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useAssistantStore } from "../store";
import {
  TOUCH_GESTURE_LABELS,
  USER_REACTION_KINDS,
  type AvatarTouchPayload,
} from "../types";

/**
 * PersonaActionBanner — K31 + K32 surface for the detached persona
 * window (``index.html#/persona``).
 *
 * The persona overlay never renders chat bubbles, so the gesture
 * badge + reaction tray that lives on ``MessageBubble`` in the main
 * window has no home there. This banner fills that gap: a small
 * transient pill that appears on every ``avatar_touch`` event and
 * lets the user reciprocate inline.
 *
 * Behaviour:
 *
 *   1. **Trigger** — subscribes to the store's ``avatarTouchAt``
 *      dedup counter (same identity key the Live2D bridge uses).
 *      A bump means "a new gesture just landed"; we pull the
 *      payload from ``avatarTouch`` and find the matching
 *      assistant message by walking the messages array backwards
 *      (assistant role, has ``backendId``). The latest one is the
 *      target — that's the bubble the gesture was attached to.
 *
 *   2. **Render** — pill with the gesture emoji + label
 *      (``"Aiko gave you a hug"``) plus the six reaction buttons
 *      from the K32 taxonomy. Reaction counts come from the same
 *      ``messages[i].reactions`` map the main window uses, so a
 *      click here updates the chat-window strip and vice versa
 *      via the ``message_reaction_updated`` WS broadcast.
 *
 *   3. **Dismiss** — auto-hides after ``durationMs`` (default 20s,
 *      configurable via ``agent.persona_touch_banner_duration_seconds``).
 *      A new gesture mid-window replaces the visible banner and
 *      resets the timer. The close button manually hides.
 *
 *   4. **Master switch** — guards on ``agent.persona_touch_banner_enabled``
 *      (the WS hello carries the settings snapshot). When disabled,
 *      the component is a no-op (returns ``null``) and never
 *      subscribes to the touch counter beyond the gate check.
 *
 * Layout note: the banner is rendered at the top of the persona
 * window content area, BELOW the drag handle. We use
 * ``position: absolute`` + ``inset-x-0 top-12`` so the banner
 * never displaces the avatar — the user always sees the rig.
 */
interface PersonaActionBannerProps {
  /** Master switch. Mirrors the server-side
   * ``agent.persona_touch_banner_enabled`` setting. ``PersonaWindow``
   * threads it in; when ``false`` the component is a no-op. Defaults
   * to ``true`` so callers that don't yet have a settings snapshot
   * still see the feature. */
  enabled?: boolean;
  /** Visibility lifetime in ms once a fresh gesture lands.
   * Defaults to 20s; ``PersonaWindow`` threads the user-tunable
   * setting in. */
  durationMs?: number;
}

interface BannerState {
  /** The gesture payload currently being displayed. */
  payload: AvatarTouchPayload;
  /** Backend message id the banner attaches reactions to, or
   * ``null`` when no assistant bubble has a backend id yet (e.g.
   * the very first turn before persistence completes). The
   * reaction buttons stay disabled in that case. */
  messageId: number | null;
  /** Monotonic ``Date.now()`` when the banner started. The
   * auto-dismiss timer compares against this. */
  startedAt: number;
}

const DEFAULT_DURATION_MS = 20_000;

export function PersonaActionBanner({
  enabled = true,
  durationMs = DEFAULT_DURATION_MS,
}: PersonaActionBannerProps) {
  const avatarTouchAt = useAssistantStore((s) => s.avatarTouchAt);
  const avatarTouch = useAssistantStore((s) => s.avatarTouch);
  const messages = useAssistantStore((s) => s.messages);
  const applyMessageReactions = useAssistantStore(
    (s) => s.applyMessageReactions,
  );
  const pushToast = useAssistantStore((s) => s.pushToast);

  const [banner, setBanner] = useState<BannerState | null>(null);
  const [reactBusyKind, setReactBusyKind] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Hide on a "manual" close click. Symmetric with the auto-timer.
  const dismiss = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setBanner(null);
  }, []);

  // Re-arm the banner whenever a fresh gesture lands.
  useEffect(() => {
    if (!enabled) {
      if (banner) {
        dismiss();
      }
      return;
    }
    if (avatarTouchAt <= 0 || !avatarTouch) {
      return;
    }
    // Find the latest assistant bubble with a backendId — the
    // gesture is attached to whichever turn just produced it.
    let messageId: number | null = null;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i];
      if (m.role === "assistant" && m.backendId != null) {
        messageId = m.backendId;
        break;
      }
    }
    setBanner({
      payload: avatarTouch,
      messageId,
      startedAt: Date.now(),
    });
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      setBanner(null);
      timerRef.current = null;
    }, Math.max(1000, durationMs));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [avatarTouchAt, enabled, durationMs]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  // The live reactions map for the message the banner is attached
  // to. Pulled fresh from the messages slice so a server-side
  // ``message_reaction_updated`` broadcast updates the persona
  // banner counters in real time.
  const liveReactions = useMemo(() => {
    if (!banner || banner.messageId == null) return {};
    const row = messages.find((m) => m.backendId === banner.messageId);
    return row?.reactions ?? {};
  }, [banner, messages]);

  const onToggleReaction = useCallback(
    async (kindClicked: string) => {
      if (!banner || banner.messageId == null) return;
      const messageId = banner.messageId;
      const current = { ...liveReactions };
      const has = (current[kindClicked] ?? 0) > 0;
      setReactBusyKind(kindClicked);
      try {
        if (has) {
          const next = { ...current };
          delete next[kindClicked];
          applyMessageReactions(messageId, next);
          const result = await api.removeReaction(messageId, kindClicked);
          applyMessageReactions(messageId, result.reactions ?? {});
        } else {
          const next = {
            ...current,
            [kindClicked]: (current[kindClicked] ?? 0) + 1,
          };
          applyMessageReactions(messageId, next);
          const result = await api.addReaction(messageId, kindClicked);
          applyMessageReactions(messageId, result.reactions ?? {});
        }
      } catch (err) {
        applyMessageReactions(messageId, current);
        pushToast("warning", `Reaction failed: ${String(err)}`);
      } finally {
        setReactBusyKind(null);
      }
    },
    [banner, liveReactions, applyMessageReactions, pushToast],
  );

  if (!enabled || !banner) {
    return null;
  }
  const meta = TOUCH_GESTURE_LABELS[banner.payload.kind] ?? {
    label: banner.payload.label || banner.payload.kind,
    emoji: banner.payload.emoji || "✨",
  };
  const labelText =
    banner.payload.label && banner.payload.label.trim().length > 0
      ? banner.payload.label
      : `Aiko ${meta.label}`;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="persona-action-banner"
      className="pointer-events-auto absolute inset-x-2 top-12 z-30 mx-auto flex max-w-md items-center gap-2 rounded-xl border border-pink-400/40 bg-black/70 px-3 py-2 text-sm text-pink-50 shadow-xl backdrop-blur"
    >
      <span className="text-lg">{meta.emoji}</span>
      <span className="flex-1 truncate" title={labelText}>
        {labelText}
      </span>
      <div className="flex items-center gap-1">
        {USER_REACTION_KINDS.map((r) => {
          const has = (liveReactions[r.kind] ?? 0) > 0;
          return (
            <button
              key={r.kind}
              type="button"
              disabled={banner.messageId == null || reactBusyKind != null}
              onClick={() => {
                void onToggleReaction(r.kind);
              }}
              title={r.label}
              className={`rounded-md px-1.5 py-0.5 text-[14px] transition-colors ${
                has
                  ? "bg-pink-500/30 text-pink-50"
                  : "text-ink-100/70 hover:bg-white/10 hover:text-ink-100"
              } disabled:opacity-40`}
            >
              {r.emoji}
            </button>
          );
        })}
      </div>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss"
        className="ml-1 flex h-5 w-5 items-center justify-center rounded text-ink-100/50 hover:bg-white/10 hover:text-ink-100"
      >
        ×
      </button>
    </div>
  );
}
