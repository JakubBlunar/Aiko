import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useAssistantStore } from "../store";
import type { ToolEvent, VoiceMode, WsClientCommand } from "../types";
import { SHARED_MOMENT_VIBES } from "../types";
import { ContextBadge } from "./ContextBadge";
import { MicButton } from "./MicButton";

interface ChatViewProps {
  send: (cmd: WsClientCommand) => void;
}

const REACTION_EMOJI: Record<string, string> = {
  cheerful: "😊",
  excited: "✨",
  enthusiastic: "🤩",
  friendly: "🙂",
  calm: "😌",
  serious: "🤔",
  sad: "😔",
  gentle: "🌸",
  angry: "😠",
  surprised: "😮",
  confused: "😵‍💫",
  // Phase 5 (expression overhaul) reaction emojis. Chosen for the
  // chat transcript pip — they share styling space with the rest
  // of the small reaction icons so the user can scan their own
  // mood arc.
  embarrassed: "☺️",
  nervous: "😰",
  defiant: "😤",
  neutral: "🌙",
};

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

export function ChatView({ send }: ChatViewProps) {
  const messages = useAssistantStore((s) => s.messages);
  const status = useAssistantStore((s) => s.status);
  const turnInProgress = useAssistantStore((s) => s.turnInProgress);
  const ttsState = useAssistantStore((s) => s.ttsState);
  const reaction = useAssistantStore((s) => s.reaction);
  const connection = useAssistantStore((s) => s.connection);
  const voiceMode = useAssistantStore((s) => s.voiceMode);
  const audioLevel = useAssistantStore((s) => s.audioLevel);
  const lastTranscript = useAssistantStore((s) => s.lastTranscript);
  const setLastTranscript = useAssistantStore((s) => s.setLastTranscript);
  const currentPartial = useAssistantStore((s) => s.currentPartial);
  const toolActivity = useAssistantStore((s) => s.toolActivity);
  const sessionKey = useAssistantStore((s) => s.sessionKey);

  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Tracks whether we've already done the "land at bottom" jump for the
  // current session. Reset when the session key or first-load completes.
  const initialScrolledRef = useRef<string | null>(null);
  // ``followingTail`` captures the user's intent: true while they are
  // pinned to (or near) the bottom; false the moment they scroll up to
  // read history. We update this from ``scroll`` events so the value
  // reflects the *previous* layout, before a new message arrives. Then
  // when a message lands, we stick to bottom iff this flag is true —
  // which sidesteps the old "compare distance against the new
  // scrollHeight" race that broke for messages taller than the
  // threshold (anything > 120 px would silently un-stick the chat).
  const followingTailRef = useRef(true);

  // Hide the transcript pill ~3s after we receive a final transcript.
  useEffect(() => {
    if (!lastTranscript) return;
    const id = window.setTimeout(() => setLastTranscript(""), 3000);
    return () => window.clearTimeout(id);
  }, [lastTranscript, setLastTranscript]);

  // Reset the "initial scrolled" guard when the session changes so that
  // switching to another conversation also jumps to its latest message.
  useEffect(() => {
    initialScrolledRef.current = null;
  }, [sessionKey]);

  // Maintain ``followingTailRef`` from the user's scroll position. We
  // use a small 32 px threshold here because the scroll listener fires
  // *during* user gestures (no race with new messages), so its only
  // job is detecting "is the user at the bottom right now?".
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      followingTailRef.current = distance < 32;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // Scroll behavior:
  //   1. On first paint with messages (or after a session switch), jump
  //      straight to the bottom — the user wants to land on the most
  //      recent message.
  //   2. On subsequent updates (streaming tokens, new turn), only stick
  //      to the bottom if the user was already at the tail just before
  //      this update landed. ``followingTailRef`` is set from the
  //      scroll listener above, so it reflects intent — not a heuristic
  //      against the now-stale post-render geometry.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || messages.length === 0) return;

    const sessionTag = sessionKey || "__default__";
    if (initialScrolledRef.current !== sessionTag) {
      initialScrolledRef.current = sessionTag;
      // Two raf ticks: first lets layout settle (bubbles + auto-grown
      // textarea), second performs the jump after final heights are known.
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const node = scrollRef.current;
          if (node) {
            node.scrollTop = node.scrollHeight;
            followingTailRef.current = true;
          }
        });
      });
      return;
    }

    if (followingTailRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages, sessionKey]);

  // Auto-grow the input textarea up to its max-height so the row doesn't
  // start out as a tall 2-line box but still expands naturally.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = 160; // matches max-h-40 in the className
    const next = Math.min(max, Math.max(48, el.scrollHeight));
    el.style.height = `${next}px`;
  }, [draft]);

  const headerReaction = useMemo(() => {
    return REACTION_EMOJI[reaction] ?? REACTION_EMOJI.neutral;
  }, [reaction]);

  const handleSend = () => {
    const text = draft.trim();
    if (!text || turnInProgress || connection.status !== "connected") {
      return;
    }
    send({ type: "chat", text });
    setDraft("");
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSend();
    }
  };

  const handleMicToggle = () => {
    if (connection.status !== "connected") return;
    if (voiceMode === "off") {
      send({ type: "voice_start" });
    } else {
      send({ type: "voice_stop" });
    }
  };

  const headerStatus =
    voiceMode !== "off"
      ? `Voice: ${voiceMode}`
      : ttsState === "speaking"
        ? "Speaking..."
        : "Idle";

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      {/* Header strip showing TTS state + reaction */}
      <div className="flex items-center justify-between border-b border-white/5 bg-white/[0.02] px-6 py-3">
        <div className="flex items-center gap-3 text-sm text-ink-100/80">
          <span className="text-2xl leading-none">{headerReaction}</span>
          <div>
            <div className="font-medium text-ink-100">Aiko</div>
            <div className="text-xs text-ink-100/60">
              {headerStatus}
              {status ? ` · ${status}` : ""}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <ContextBadge />
          <ConnectionBadge />
        </div>
      </div>

      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-6 py-8"
      >
        {messages.length === 0 ? (
          <EmptyState booting={connection.status !== "connected"} />
        ) : (
          <ul className="mx-auto flex max-w-3xl flex-col gap-4">
            {messages.map((msg) => (
              <MessageBubble key={msg.id} {...msg} />
            ))}
          </ul>
        )}
        {toolActivity.length > 0 && turnInProgress ? (
          <ToolActivityStrip activity={toolActivity} />
        ) : null}
      </div>

      <div className="border-t border-white/5 bg-white/[0.02] px-6 py-4">
        <div className="mx-auto max-w-3xl">
          <VoiceStrip
            voiceMode={voiceMode}
            audioLevel={audioLevel}
            lastTranscript={lastTranscript}
            currentPartial={currentPartial}
          />
          <div className="flex items-center gap-2">
            <MicButton
              voiceMode={voiceMode}
              audioLevel={audioLevel}
              connected={connection.status === "connected"}
              onClick={handleMicToggle}
            />
            <textarea
              ref={textareaRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                connection.status !== "connected"
                  ? "Connecting..."
                  : voiceMode !== "off"
                    ? "Voice mode is on. Type to send a written message, or click the mic to stop."
                    : "Talk to Aiko... (Enter to send, Shift+Enter for newline)"
              }
              disabled={connection.status !== "connected"}
              rows={1}
              className="h-12 max-h-40 min-h-[3rem] flex-1 resize-none self-center rounded-xl border border-white/10 bg-black/30 px-4 py-3 text-sm leading-6 text-ink-100 placeholder:text-ink-100/40 focus:border-ink-400 focus:outline-none focus:ring-2 focus:ring-ink-500/40 disabled:opacity-60"
            />
            {turnInProgress ? (
              <button
                type="button"
                onClick={() => send({ type: "stop" })}
                className="flex h-12 min-w-[3rem] shrink-0 items-center justify-center rounded-xl bg-red-500/80 px-4 text-sm font-medium text-white transition hover:bg-red-500"
                title="Stop generation"
                aria-label="Stop generation"
              >
                Stop
              </button>
            ) : (
              <button
                type="button"
                onClick={handleSend}
                disabled={!draft.trim() || connection.status !== "connected"}
                className="flex h-12 min-w-[3rem] shrink-0 items-center justify-center rounded-xl bg-ink-500 px-5 text-sm font-medium text-white transition hover:bg-ink-400 disabled:cursor-not-allowed disabled:bg-white/10 disabled:text-white/40"
                title="Send message (Enter)"
                aria-label="Send message"
              >
                Send
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

interface VoiceStripProps {
  voiceMode: VoiceMode;
  audioLevel: number;
  lastTranscript: string;
  currentPartial: string;
}

function VoiceStrip({
  voiceMode,
  audioLevel,
  lastTranscript,
  currentPartial,
}: VoiceStripProps) {
  const isOn = voiceMode !== "off";
  if (!isOn && !lastTranscript && !currentPartial) {
    return null;
  }
  const labelMap: Record<VoiceStripProps["voiceMode"], string> = {
    off: "Voice off",
    listening: "Listening",
    transcribing: "Transcribing…",
    thinking: "Thinking…",
    speaking: "Speaking…",
  };
  // Show the live partial only while we're actively listening — once
  // the user has stopped talking the "you said:" pill takes over.
  const showPartial = currentPartial && voiceMode === "listening";
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
      {isOn ? (
        <span className="inline-flex items-center gap-2 rounded-full border border-pink-400/30 bg-pink-500/10 px-3 py-1 text-pink-100">
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 rounded-full bg-pink-300"
            style={{
              opacity: 0.4 + Math.min(1, audioLevel) * 0.6,
              transition: "opacity 80ms linear",
            }}
          />
          <span className="font-medium uppercase tracking-wider">
            {labelMap[voiceMode]}
          </span>
          <AudioMeter level={audioLevel} active={voiceMode === "listening"} />
        </span>
      ) : null}
      {showPartial ? (
        <span className="inline-flex max-w-full items-center gap-2 truncate rounded-full border border-pink-400/20 bg-pink-500/5 px-3 py-1 text-pink-100/70">
          <span className="text-pink-100/40">hearing:</span>
          <span className="italic truncate">{currentPartial}…</span>
        </span>
      ) : null}
      {lastTranscript ? (
        <span className="inline-flex max-w-full items-center gap-2 truncate rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-ink-100/70">
          <span className="text-ink-100/40">you said:</span>
          <span className="italic truncate">{lastTranscript}</span>
        </span>
      ) : null}
    </div>
  );
}

function AudioMeter({ level, active }: { level: number; active: boolean }) {
  const bars = 4;
  const filled = Math.round(Math.max(0, Math.min(1, level)) * bars);
  return (
    <div className="flex h-1.5 items-end gap-0.5" aria-hidden="true">
      {Array.from({ length: bars }).map((_, i) => {
        const isLit = active && i < filled;
        const height = active ? 4 + i * 2 : 4;
        return (
          <span
            key={i}
            className={`w-0.5 rounded-sm transition-colors ${
              isLit ? "bg-pink-400" : "bg-white/10"
            }`}
            style={{ height: `${height}px` }}
          />
        );
      })}
    </div>
  );
}

function ConnectionBadge() {
  const status = useAssistantStore((s) => s.connection.status);
  const tone =
    status === "connected"
      ? "bg-emerald-500/20 text-emerald-200 border-emerald-400/40"
      : status === "connecting"
        ? "bg-amber-400/20 text-amber-100 border-amber-300/40"
        : "bg-rose-500/20 text-rose-200 border-rose-400/40";
  const label =
    status === "connected"
      ? "online"
      : status === "connecting"
        ? "connecting"
        : "offline";
  return (
    <span
      className={`rounded-full border px-3 py-1 text-xs font-medium ${tone}`}
    >
      {label}
    </span>
  );
}

const TOOL_LABELS: Record<string, { call: string; result: string; icon: string }> = {
  get_time: {
    call: "checking the time",
    result: "got the current time",
    icon: "⏱️",
  },
  recall: {
    call: "searching her notebook",
    result: "found something in her notebook",
    icon: "📔",
  },
  web_search: {
    call: "searching the web",
    result: "found something on the web",
    icon: "🔎",
  },
};

function ToolActivityStrip({ activity }: { activity: ToolEvent[] }) {
  if (activity.length === 0) return null;
  const items = activity.slice(-4);
  return (
    <ul className="mx-auto mt-3 flex max-w-3xl flex-col gap-1 text-xs text-ink-100/55">
      {items.map((evt, idx) => {
        const meta = TOOL_LABELS[evt.name] ?? {
          call: `running ${evt.name}`,
          result: `${evt.name} returned`,
          icon: "🛠",
        };
        const failed = evt.event === "result" && evt.ok === false;
        const phrase = evt.event === "call" ? meta.call : failed ? `${evt.name} failed` : meta.result;
        return (
          <li
            key={`${evt.name}-${evt.at}-${idx}`}
            className={`flex items-center gap-2 ${failed ? "text-rose-300/80" : ""}`}
          >
            <span aria-hidden="true">{meta.icon}</span>
            <span>aiko is {phrase}…</span>
          </li>
        );
      })}
    </ul>
  );
}

function EmptyState({ booting = false }: { booting?: boolean }) {
  // While the WS hasn't opened yet we show a "still connecting" hint
  // instead of the cheerful greeting. The greeting promises that the
  // user can type and get a reply, which isn't true until the backend
  // answers — see ``useAssistantSocket`` for the matching state.
  if (booting) {
    return (
      <div
        className="mx-auto mt-24 max-w-md text-center"
        role="status"
        aria-live="polite"
      >
        <div
          className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-ink-100/20 border-t-ink-100/70"
          aria-hidden="true"
        />
        <h2 className="text-lg font-semibold text-ink-100">
          Waiting for Aiko…
        </h2>
        <p className="mt-2 text-sm text-ink-100/60">
          The desktop runtime is still starting the backend. This usually
          takes a few seconds; the chat will unlock as soon as the server
          answers.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto mt-24 max-w-md text-center">
      <div className="text-5xl">🌸</div>
      <h2 className="mt-4 text-lg font-semibold text-ink-100">
        Hi, I'm Aiko.
      </h2>
      <p className="mt-2 text-sm text-ink-100/60">
        I'm here to chat about whatever's on your mind. Random thoughts,
        what you're working on, something you saw earlier today — drop a
        line and I'll pick up the thread. Speech in and speech out are
        wired through the desktop runtime, so I'll talk back through your
        speakers.
      </p>
    </div>
  );
}

interface BubbleProps {
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  streaming?: boolean;
  reaction?: string;
  kind?: "proactive";
  /** Backend message id (when known) — gates the "Mark as moment" action. */
  backendId?: number;
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

function MessageBubble({
  role,
  content,
  createdAt,
  streaming,
  reaction,
  kind,
  backendId,
}: BubbleProps) {
  const [markOpen, setMarkOpen] = useState(false);
  const [marking, setMarking] = useState(false);
  const [marked, setMarked] = useState(false);
  const pushToast = useAssistantStore((s) => s.pushToast);

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

  if (role === "system") {
    return (
      <li className="mx-auto max-w-md text-center text-xs italic text-ink-100/40">
        {content}
      </li>
    );
  }

  const isUser = role === "user";
  const isProactive = !isUser && kind === "proactive";
  // Mark-as-moment is available on any persisted user/assistant row
  // (system messages excluded above). Streaming rows don't have a
  // backendId yet so the button stays hidden until the turn lands.
  const canMark = !streaming && backendId != null;
  return (
    <li
      className={`group flex flex-col gap-1 ${
        isUser ? "items-end" : "items-start"
      }`}
    >
      <div
        className={`relative whitespace-pre-wrap rounded-2xl px-4 py-3 text-sm leading-relaxed shadow-md ${
          isUser
            ? "max-w-xl bg-ink-600/80 text-white"
            : isProactive
              ? "max-w-2xl border border-emerald-400/30 bg-emerald-500/[0.08] text-ink-100"
              : "max-w-2xl border border-white/10 bg-white/[0.04] text-ink-100"
        } ${streaming ? "streaming-caret" : ""}`}
      >
        {content
          ? isUser
            ? content
            : renderMessageContent(content)
          : streaming
            ? ""
            : "(empty)"}

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
      <div className="text-[10px] text-ink-100/40">
        {isUser ? "you" : isProactive ? "aiko · proactive" : "aiko"} ·{" "}
        {formatTime(createdAt)}
        {!isUser && reaction && reaction !== "neutral" ? ` · ${reaction}` : ""}
      </div>
    </li>
  );
}
