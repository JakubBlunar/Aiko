import type { AttachmentRef, ChatMessage, TouchGestureBadge } from "@/types";
import { nextId, resetIdCounter } from "../ids";
import type { SliceCreator } from "../types";

const REACTION_TAG_RE = /\[\[reaction:(\w+)\]\]/i;

/**
 * Defense-in-depth: strip every meta marker the assistant might emit, so the
 * UI is bulletproof regardless of source (live stream, history fetch, MCP
 * message, future model swap). Mirrors
 * ``app/core/services/response_text_service.strip_all_meta_tags``.
 */
function stripMetaMarkers(s: string): string {
  if (!s) return s;
  return (
    s
      // Drop full [[detail]]...[[/detail]] blocks (and any unclosed tail).
      .replace(/\[\[detail\]\][\s\S]*?(?:\[\[\/detail\]\]|$)/gi, "")
      // Drop [[remember:...]] tags entirely (private notebook).
      .replace(/\[\[remember:[^\]]*?\]\]/gi, "")
      // Drop unclosed remember at end-of-string.
      .replace(/\[\[remember:[^\]]*$/gi, "")
      // Strip [[spoken]]/[[/spoken]] markers (keep content).
      .replace(/\[\[\/?spoken\]\]/gi, "")
      // Strip [[reaction:X]] markers (kept separately as state).
      .replace(/\[\[reaction:\w+\]\]/gi, "")
      // Collapse runaway blank lines left over from removed blocks.
      .replace(/\n{3,}/g, "\n\n")
  );
}

export interface ChatSlice {
  // Chat transcript
  messages: ChatMessage[];
  /** P9: per-turn draft for the active assistant bubble. Streamed tokens
   * land here (one O(1) write per chunk) instead of cloning ``messages``
   * per token. ``finishAssistantBubble`` commits it; any transcript wipe
   * clears it. ``null`` between turns. */
  streamingDraft: {
    id: string;
    content: string;
    reaction: string | undefined;
  } | null;
  setMessages: (msgs: ChatMessage[]) => void;
  /** I6: prepend an older page of history (keyset pagination); skips rows
   * whose ``backendId`` is already present. */
  prependMessages: (msgs: ChatMessage[]) => void;
  /** I6: whether older history pages may still exist for this session. */
  historyHasMore: boolean;
  setHistoryHasMore: (value: boolean) => void;
  appendUserMessage: (content: string) => void;
  appendAssistantBubble: () => string; // returns id
  appendAssistantToken: (chunk: string) => void;
  finishAssistantBubble: () => void;
  /** K32: stamp the just-finished assistant bubble with its persisted
   * SQLite ``messages.id`` (delivered on ``turn_done``). */
  stampAssistantBackendId: (backendId: number | null | undefined) => void;
  appendProactiveMessage: (content: string, backendId?: number) => void;
  pushSystemMessage: (content: string) => void;
  clearMessages: () => void;
  /** K32: merge a fresh reactions counter map onto the matching message
   * (matched by ``backendId``). */
  applyMessageReactions: (
    backendId: number,
    reactions: Record<string, number>,
  ) => void;
  /** D2 Part B: stamp attachments onto the most recent user bubble. */
  attachLastUserAttachments: (attachments: AttachmentRef[]) => void;
  /** K31: stamp the kinds Aiko emitted this turn onto an assistant bubble. */
  appendGestureToCurrentTurn: (badge: string | TouchGestureBadge) => void;
}

export const createChatSlice: SliceCreator<ChatSlice> = (set) => ({
  messages: [],
  streamingDraft: null,
  setMessages: (msgs) =>
    set({
      messages: msgs.map((m) =>
        m.role === "assistant"
          ? { ...m, content: stripMetaMarkers(m.content) }
          : m,
      ),
      // History reload nukes any in-flight draft -- the bubble it
      // referenced is gone, and a fresh ``setMessages`` always lands
      // outside a turn.
      streamingDraft: null,
    }),
  prependMessages: (msgs) =>
    set((state) => {
      if (msgs.length === 0) return state;
      const known = new Set(
        state.messages
          .map((m) => m.backendId)
          .filter((id): id is number => id != null),
      );
      const fresh = msgs
        .filter((m) => m.backendId == null || !known.has(m.backendId))
        .map((m) =>
          m.role === "assistant"
            ? { ...m, content: stripMetaMarkers(m.content) }
            : m,
        );
      if (fresh.length === 0) return state;
      return { messages: [...fresh, ...state.messages] };
    }),
  historyHasMore: false,
  setHistoryHasMore: (value) => set({ historyHasMore: Boolean(value) }),
  appendUserMessage: (content) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: nextId(),
          role: "user",
          content,
          createdAt: new Date().toISOString(),
        },
      ],
    })),
  appendAssistantBubble: () => {
    const id = nextId();
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id,
          role: "assistant",
          // Mid-stream content lives in ``streamingDraft``; the
          // placeholder's ``content`` stays empty and is overwritten
          // once at commit. Keeps ``messages`` stable across the
          // whole turn so non-streaming bubbles never re-render.
          content: "",
          createdAt: new Date().toISOString(),
          streaming: true,
        },
      ],
      streamingDraft: { id, content: "", reaction: undefined },
    }));
    return id;
  },
  appendAssistantToken: (chunk) =>
    set((state) => {
      const draft = state.streamingDraft;
      if (!draft) {
        return state;
      }
      // ``stripMetaMarkers`` is idempotent on already-cleaned text,
      // so doing it per chunk keeps the draft canonical even when a
      // marker sits across two chunks (the half-marker tail stays in
      // ``content`` until the next chunk completes it). Cost is
      // O(m) per token same as before -- the win is that this no
      // longer drags the whole ``messages`` array along with it.
      const merged = draft.content + chunk;
      const reactionMatch = REACTION_TAG_RE.exec(merged);
      const reaction = reactionMatch
        ? reactionMatch[1].toLowerCase()
        : draft.reaction;
      const cleaned = stripMetaMarkers(merged);
      return {
        streamingDraft: { id: draft.id, content: cleaned, reaction },
      };
    }),
  finishAssistantBubble: () =>
    set((state) => {
      const draft = state.streamingDraft;
      if (state.messages.length === 0) {
        return draft ? { streamingDraft: null } : state;
      }
      const last = state.messages[state.messages.length - 1];
      if (last.role !== "assistant" || !last.streaming) {
        // No streaming bubble to commit into -- just clear any
        // stale draft so we never leak across turns.
        return draft ? { streamingDraft: null } : state;
      }
      // Two failure modes to absorb here:
      //   1. ``turn_done`` arrives before any token landed (rare but
      //      possible if the agent produces no content). ``draft``
      //      is the empty placeholder; we still flip ``streaming``.
      //   2. ``error`` mid-stream -- we want partial text to stick
      //      so the user sees what Aiko managed to say, then the
      //      bubble exits the streaming state cleanly. Same code
      //      path as a normal commit.
      const committed = draft && draft.id === last.id ? draft : null;
      return {
        messages: [
          ...state.messages.slice(0, -1),
          {
            ...last,
            content: committed ? committed.content : last.content,
            reaction: committed?.reaction ?? last.reaction,
            streaming: false,
          },
        ],
        streamingDraft: null,
      };
    }),
  stampAssistantBackendId: (backendId) =>
    set((state) => {
      if (backendId == null || state.messages.length === 0) return state;
      // Only the just-committed bubble (the last message) is a candidate.
      // Guard against clobbering an already-stamped bubble (e.g. a
      // proactive line that carried its id at append time) and against
      // empty/aborted turns where the last message is the user's.
      const idx = state.messages.length - 1;
      const last = state.messages[idx];
      if (last.role !== "assistant" || last.backendId != null) return state;
      const messages = state.messages.slice();
      messages[idx] = { ...last, backendId };
      return { messages };
    }),
  pushSystemMessage: (content) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: nextId(),
          role: "system",
          content,
          createdAt: new Date().toISOString(),
        },
      ],
    })),
  appendProactiveMessage: (content, backendId) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: nextId(),
          role: "assistant",
          content: stripMetaMarkers(content),
          createdAt: new Date().toISOString(),
          kind: "proactive",
          // K32: proactive bubbles carry their persisted id from the
          // ``message`` WS event so reactions work on them immediately.
          ...(backendId != null ? { backendId } : {}),
        },
      ],
    })),
  clearMessages: () => {
    resetIdCounter();
    set({ messages: [], streamingDraft: null });
  },
  applyMessageReactions: (backendId, reactions) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.backendId === backendId ? { ...m, reactions: { ...reactions } } : m,
      ),
    })),
  attachLastUserAttachments: (attachments) =>
    set((state) => {
      if (!attachments || attachments.length === 0) return state;
      for (let i = state.messages.length - 1; i >= 0; i -= 1) {
        if (state.messages[i].role === "user") {
          const next = [...state.messages];
          next[i] = { ...next[i], attachments: [...attachments] };
          return { messages: next };
        }
      }
      return state;
    }),
  appendGestureToCurrentTurn: (badge) =>
    set((state) => {
      // B7: accept either a bare kind string (legacy / convenience) or
      // a full ``{kind,label,emoji}`` descriptor so invented custom
      // gestures keep their model-supplied badge text.
      const descriptor: TouchGestureBadge =
        typeof badge === "string" ? { kind: badge } : badge;
      const kind = (descriptor.kind || "").trim();
      if (!kind) {
        return state;
      }
      // Prefer the streaming bubble if one is in flight (the
      // common case for an LLM-emitted touch). Otherwise stamp
      // the most recent assistant bubble (e.g. proactive or
      // MCP-forced ``send_touch``).
      const draftId = state.streamingDraft?.id;
      let targetIndex = -1;
      if (draftId) {
        targetIndex = state.messages.findIndex((m) => m.id === draftId);
      }
      if (targetIndex < 0) {
        for (let i = state.messages.length - 1; i >= 0; i -= 1) {
          if (state.messages[i].role === "assistant") {
            targetIndex = i;
            break;
          }
        }
      }
      if (targetIndex < 0) {
        return state;
      }
      const target = state.messages[targetIndex];
      const existing = target.gestures ?? [];
      // Dedup by kind regardless of stored shape (string or descriptor).
      const alreadyHasKind = existing.some(
        (g) => (typeof g === "string" ? g : g.kind) === kind,
      );
      if (alreadyHasKind) {
        return state;
      }
      const next = [...state.messages];
      next[targetIndex] = { ...target, gestures: [...existing, descriptor] };
      return { messages: next };
    }),
});
