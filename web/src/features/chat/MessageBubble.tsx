import { memo, useCallback, useState } from "react";
import { api } from "@/api";
import { useAssistantStore } from "@/store";
import type { AttachmentRef, TouchGestureBadge } from "@/types";
import {
  normalizeGesture,
  SHARED_MOMENT_VIBES,
  USER_REACTION_KINDS,
} from "@/types";
import { attachmentUrl, formatTime } from "./chatFormat";

interface BubbleProps {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  streaming?: boolean;
  reaction?: string;
  kind?: "proactive";
  /** Backend message id (when known) — gates the "Mark as moment" action. */
  backendId?: number;
  /** K31 / B7: touch gestures Aiko emitted on this turn (one badge per
   * entry, in order). Each entry is a legacy bare-``kind`` string or a
   * ``{kind,label,emoji}`` descriptor. Empty / undefined for plain
   * bubbles. */
  gestures?: (string | TouchGestureBadge)[];
  /** K32: counter map of user-reaction kinds clicked on this
   * bubble. The hover tray + persistent strip both render this. */
  reactions?: Record<string, number>;
  /** D2 Part B: files the user attached to this (user) message. */
  attachments?: AttachmentRef[];
}

// Phase 3c: parse `[[correct]]old[[/correct]]new` into renderable
// pieces. ``new`` is the text immediately following the close tag,
// up to the next sentence-ending punctuation or string end. We render
// ``old`` as a strikethrough span and ``new`` as plain text so the
// reader sees Aiko catching her own slip.
type CorrectionPiece =
  | { kind: "text"; value: string }
  | { kind: "correction"; old: string };

function parseCorrections(text: string): CorrectionPiece[] {
  if (!text) {
    return [];
  }
  const out: CorrectionPiece[] = [];
  const re = /\[\[correct\]\]([\s\S]*?)\[\[\/correct\]\]/gi;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) {
      out.push({ kind: "text", value: text.slice(last, match.index) });
    }
    out.push({ kind: "correction", old: match[1] });
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    out.push({ kind: "text", value: text.slice(last) });
  }
  return out;
}

function renderMessageContent(content: string): React.ReactNode {
  const pieces = parseCorrections(content);
  if (pieces.length <= 1) {
    return content;
  }
  return pieces.map((piece, idx) => {
    if (piece.kind === "correction") {
      return (
        <span
          key={idx}
          className="text-ink-100/45 line-through decoration-ink-100/40 mr-1"
          title="Aiko corrected herself"
        >
          {piece.old}
        </span>
      );
    }
    return <span key={idx}>{piece.value}</span>;
  });
}

// ``MessageBubble`` is wrapped in :func:`memo` below because the
// streaming bubble subscribes to ``streamingDraft`` directly (P9):
// the messages array stays referentially stable across the whole
// turn so Virtuoso never re-keys, but the streaming bubble alone
// re-renders per token via its own draft subscription. Non-
// streaming bubbles get a stable ``null`` from the selector and
// memo skips their re-render entirely. Without memo, every prop
// fan-out from ``ChatView`` (e.g. on session switch or post-turn
// metadata) would still re-render every visible bubble.
function MessageBubbleImpl({
  id,
  role,
  content,
  createdAt,
  streaming,
  reaction,
  kind,
  backendId,
  gestures,
  reactions,
  attachments,
}: BubbleProps) {
  // P9: when this bubble is the active streaming bubble, pull its
  // live content + reaction from the ``streamingDraft`` slice
  // instead of the props. The selector returns the SAME ``null``
  // reference for every other bubble (and between turns), so the
  // store update fan-out leaves them untouched. Only the streaming
  // bubble re-renders per token. On commit (``finishAssistantBubble``)
  // the draft clears and the props become authoritative again.
  const draft = useAssistantStore((s) =>
    s.streamingDraft && s.streamingDraft.id === id ? s.streamingDraft : null,
  );
  const liveContent = draft ? draft.content : content;
  const liveReaction = draft?.reaction ?? reaction;
  const [markOpen, setMarkOpen] = useState(false);
  const [marking, setMarking] = useState(false);
  const [marked, setMarked] = useState(false);
  const [reactBusyKind, setReactBusyKind] = useState<string | null>(null);
  const pushToast = useAssistantStore((s) => s.pushToast);
  const applyMessageReactions = useAssistantStore(
    (s) => s.applyMessageReactions,
  );

  const onMark = useCallback(
    async (vibe: string) => {
      if (backendId == null) return;
      setMarking(true);
      try {
        await api.markMessageAsMoment(backendId, vibe);
        setMarked(true);
        pushToast("memory", "Saved as a shared moment");
      } catch (err) {
        pushToast("warning", `Couldn't save moment: ${String(err)}`);
      } finally {
        setMarking(false);
        setMarkOpen(false);
      }
    },
    [backendId, pushToast],
  );

  // K32: react / un-react on this bubble. Optimistic: we update the
  // store immediately, then reconcile with the server response (or
  // roll back on error). The WS broadcast lands a moment later and
  // is idempotent.
  const onToggleReaction = useCallback(
    async (kindClicked: string) => {
      if (backendId == null) return;
      const current = reactions ?? {};
      const has = (current[kindClicked] ?? 0) > 0;
      setReactBusyKind(kindClicked);
      try {
        if (has) {
          const next = { ...current };
          delete next[kindClicked];
          applyMessageReactions(backendId, next);
          const result = await api.removeReaction(backendId, kindClicked);
          applyMessageReactions(backendId, result.reactions ?? {});
        } else {
          const next = { ...current, [kindClicked]: (current[kindClicked] ?? 0) + 1 };
          applyMessageReactions(backendId, next);
          const result = await api.addReaction(backendId, kindClicked);
          applyMessageReactions(backendId, result.reactions ?? {});
        }
      } catch (err) {
        // Roll back to the pre-click state. The WS broadcast is the
        // ultimate source of truth; if it never comes the optimistic
        // value sits, but at least the user sees an error toast.
        applyMessageReactions(backendId, current);
        pushToast("warning", `Reaction failed: ${String(err)}`);
      } finally {
        setReactBusyKind(null);
      }
    },
    [backendId, reactions, applyMessageReactions, pushToast],
  );

  if (role === "system") {
    return (
      <div className="mx-auto max-w-md text-center text-xs italic text-ink-100/40">
        {content}
      </div>
    );
  }

  const isUser = role === "user";
  const isProactive = !isUser && kind === "proactive";
  // Mark-as-moment is available on any persisted user/assistant row
  // (system messages excluded above). Streaming rows don't have a
  // backendId yet so the button stays hidden until the turn lands.
  const canMark = !streaming && backendId != null;
  // K32: reactions are only meaningful on persisted assistant
  // bubbles (the persistence column is on ``messages.reactions``).
  const canReact = !isUser && !streaming && backendId != null;
  // K31: only render the gesture footer on assistant bubbles that
  // actually fired one.
  const gestureKinds = !isUser && gestures ? gestures : [];
  // K32: pre-compute the rendered counter strip so we don't
  // recompute on every hover state change.
  const reactionEntries = Object.entries(reactions ?? {}).filter(
    ([, count]) => (count ?? 0) > 0,
  );
  return (
    <div
      role="listitem"
      className={`group flex flex-col gap-1 ${
        isUser ? "items-end" : "items-start"
      }`}
    >
      <div
        className={`relative whitespace-pre-wrap break-words rounded-2xl px-4 py-3 text-sm leading-relaxed shadow-md ${
          isUser
            ? "max-w-xl bg-ink-600/80 text-white"
            : isProactive
              ? "max-w-2xl border border-emerald-400/30 bg-emerald-500/[0.08] text-ink-100"
              : "max-w-2xl border border-white/10 bg-white/[0.04] text-ink-100"
        } ${streaming ? "streaming-caret" : ""}`}
      >
        {liveContent
          ? isUser
            ? liveContent
            : renderMessageContent(liveContent)
          : streaming
            ? ""
            : "(empty)"}

        {isUser && attachments && attachments.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-2">
            {attachments.map((att) =>
              att.kind === "image" ? (
                <a
                  key={att.rel_path}
                  href={attachmentUrl(att.rel_path)}
                  target="_blank"
                  rel="noreferrer"
                  title={att.filename}
                >
                  <img
                    src={attachmentUrl(att.rel_path)}
                    alt={att.filename}
                    className="max-h-40 rounded-lg border border-white/10 object-cover"
                  />
                </a>
              ) : (
                <a
                  key={att.rel_path}
                  href={attachmentUrl(att.rel_path)}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-2 rounded-lg border border-white/15 bg-black/20 px-3 py-2 text-xs text-white/90 hover:bg-black/30"
                  title={att.filename}
                >
                  <span className="text-base">📄</span>
                  <span className="max-w-[12rem] truncate">{att.filename}</span>
                </a>
              ),
            )}
          </div>
        ) : null}

        {canMark ? (
          <div className="absolute -top-2 right-2 opacity-0 transition-opacity group-hover:opacity-100">
            {marked ? (
              <span
                className="rounded-md bg-pink-500/30 px-2 py-0.5 text-[10px] text-pink-50"
                title="Saved to Together → Shared moments"
              >
                ★ saved
              </span>
            ) : markOpen ? (
              <div className="flex items-center gap-1 rounded-md border border-white/15 bg-black/70 px-2 py-1 shadow-lg">
                <span className="text-[10px] text-ink-100/55">vibe:</span>
                {SHARED_MOMENT_VIBES.map((v) => (
                  <button
                    key={v}
                    type="button"
                    disabled={marking}
                    onClick={() => {
                      void onMark(v);
                    }}
                    className="rounded px-1 text-[10px] text-ink-100/80 hover:bg-white/10 disabled:opacity-40"
                    title={v}
                  >
                    {v}
                  </button>
                ))}
                <button
                  type="button"
                  onClick={() => setMarkOpen(false)}
                  className="ml-1 rounded px-1 text-[10px] text-ink-100/40 hover:bg-white/10"
                >
                  ✕
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setMarkOpen(true)}
                className="rounded-md border border-white/10 bg-black/40 px-2 py-0.5 text-[10px] text-ink-100/70 hover:bg-white/10"
                title="Save this exchange as a shared moment"
              >
                ★ mark as moment
              </button>
            )}
          </div>
        ) : null}
      </div>
      {gestureKinds.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1 self-start">
          {gestureKinds.map((g, idx) => {
            const meta = normalizeGesture(g);
            return (
              <span
                key={`${meta.kind}-${idx}`}
                className="rounded-full border border-pink-400/30 bg-pink-500/[0.12] px-2 py-0.5 text-[10px] text-pink-50"
                title={`Aiko ${meta.label}`}
              >
                <span className="mr-1">{meta.emoji}</span>
                Aiko {meta.label}
              </span>
            );
          })}
        </div>
      ) : null}
      {canReact ? (
        <div className="flex flex-wrap items-center gap-1 self-start">
          {reactionEntries.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1">
              {reactionEntries.map(([kindKey, count]) => {
                const meta = USER_REACTION_KINDS.find(
                  (r) => r.kind === kindKey,
                );
                if (!meta) return null;
                return (
                  <button
                    key={kindKey}
                    type="button"
                    disabled={reactBusyKind != null}
                    onClick={() => {
                      void onToggleReaction(kindKey);
                    }}
                    title={`${meta.label} (click to remove)`}
                    className="inline-flex items-center gap-1 rounded-full border border-pink-400/40 bg-pink-500/[0.18] px-2 py-0.5 text-[11px] text-pink-50 hover:bg-pink-500/30 disabled:opacity-50"
                  >
                    <span>{meta.emoji}</span>
                    {count > 1 ? (
                      <span className="text-[10px] text-pink-100/80">
                        {count}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          ) : null}
          <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
            {USER_REACTION_KINDS.map((r) => {
              const has = (reactions?.[r.kind] ?? 0) > 0;
              if (has) return null;
              return (
                <button
                  key={r.kind}
                  type="button"
                  disabled={reactBusyKind != null}
                  onClick={() => {
                    void onToggleReaction(r.kind);
                  }}
                  title={r.label}
                  className="rounded-full px-1 py-0.5 text-[12px] text-ink-100/60 hover:bg-white/10 hover:text-ink-100 disabled:opacity-40"
                >
                  {r.emoji}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
      <div className="text-[10px] text-ink-100/40">
        {isUser ? "you" : isProactive ? "aiko · proactive" : "aiko"} ·{" "}
        {formatTime(createdAt)}
        {!isUser && liveReaction && liveReaction !== "neutral"
          ? ` · ${liveReaction}`
          : ""}
      </div>
    </div>
  );
}

export type { BubbleProps };
export const MessageBubble = memo(MessageBubbleImpl);
