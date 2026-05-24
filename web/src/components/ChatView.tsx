import { useEffect, useMemo, useRef, useState } from "react";
import { useAssistantStore } from "../store";
import type { ToolEvent, WsClientCommand } from "../types";
import { ContextBadge } from "./ContextBadge";

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
  const toolActivity = useAssistantStore((s) => s.toolActivity);

  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Hide the transcript pill ~3s after we receive a final transcript.
  useEffect(() => {
    if (!lastTranscript) return;
    const id = window.setTimeout(() => setLastTranscript(""), 3000);
    return () => window.clearTimeout(id);
  }, [lastTranscript, setLastTranscript]);

  // Auto-scroll to the bottom whenever new tokens arrive (only if user
  // already had the view scrolled to within ~100px of the end).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const stickToBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (stickToBottom) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

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
          <EmptyState />
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
          {voiceMode !== "off" && lastTranscript ? (
            <div className="mb-2 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-xs text-ink-100/70">
              <span className="text-ink-100/40">you said:</span>
              <span className="italic">{lastTranscript}</span>
            </div>
          ) : null}
          <div className="flex items-end gap-2">
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
              rows={2}
              className="flex-1 resize-none rounded-xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-ink-100 placeholder:text-ink-100/40 focus:border-ink-400 focus:outline-none focus:ring-2 focus:ring-ink-500/40 disabled:opacity-60"
            />
            {turnInProgress ? (
              <button
                type="button"
                onClick={() => send({ type: "stop" })}
                className="h-12 rounded-xl bg-red-500/80 px-4 text-sm font-medium text-white transition hover:bg-red-500"
              >
                Stop
              </button>
            ) : (
              <button
                type="button"
                onClick={handleSend}
                disabled={!draft.trim() || connection.status !== "connected"}
                className="h-12 rounded-xl bg-ink-500 px-5 text-sm font-medium text-white transition hover:bg-ink-400 disabled:cursor-not-allowed disabled:bg-white/10 disabled:text-white/40"
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

interface MicButtonProps {
  voiceMode: "off" | "listening" | "transcribing" | "thinking" | "speaking";
  audioLevel: number;
  connected: boolean;
  onClick: () => void;
}

function MicButton({
  voiceMode,
  audioLevel,
  connected,
  onClick,
}: MicButtonProps) {
  const isOn = voiceMode !== "off";
  const labelMap: Record<MicButtonProps["voiceMode"], string> = {
    off: "Voice off",
    listening: "Listening",
    transcribing: "Transcribing",
    thinking: "Thinking",
    speaking: "Speaking",
  };
  const label = labelMap[voiceMode];

  return (
    <div className="flex flex-col items-center gap-1">
      <button
        type="button"
        onClick={onClick}
        disabled={!connected}
        title={isOn ? "Stop voice mode" : "Start voice mode"}
        className={`relative flex h-12 w-12 items-center justify-center rounded-xl border text-xl transition ${
          isOn
            ? "border-pink-400/60 bg-pink-500/20 text-pink-100 hover:bg-pink-500/30"
            : "border-white/10 bg-black/30 text-ink-100/70 hover:border-ink-400 hover:text-ink-100"
        } disabled:cursor-not-allowed disabled:opacity-40`}
      >
        {isOn && voiceMode === "listening" ? (
          <span
            className="absolute inset-0 rounded-xl border-2 border-pink-400/40"
            style={{
              transform: `scale(${1 + Math.min(audioLevel, 1) * 0.25})`,
              transition: "transform 60ms linear",
              opacity: 0.6,
            }}
          />
        ) : null}
        <span className="relative">{isOn ? "🎙️" : "🎤"}</span>
      </button>
      <AudioMeter level={isOn ? audioLevel : 0} active={isOn} />
      <div
        className={`text-[10px] uppercase tracking-wider ${
          isOn ? "text-pink-200/80" : "text-ink-100/40"
        }`}
      >
        {label}
      </div>
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

function EmptyState() {
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
}

function MessageBubble({
  role,
  content,
  createdAt,
  streaming,
  reaction,
  kind,
}: BubbleProps) {
  if (role === "system") {
    return (
      <li className="mx-auto max-w-md text-center text-xs italic text-ink-100/40">
        {content}
      </li>
    );
  }

  const isUser = role === "user";
  const isProactive = !isUser && kind === "proactive";
  return (
    <li
      className={`flex flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}
    >
      <div
        className={`whitespace-pre-wrap rounded-2xl px-4 py-3 text-sm leading-relaxed shadow-md ${
          isUser
            ? "max-w-xl bg-ink-600/80 text-white"
            : isProactive
              ? "max-w-2xl border border-emerald-400/30 bg-emerald-500/[0.08] text-ink-100"
              : "max-w-2xl border border-white/10 bg-white/[0.04] text-ink-100"
        } ${streaming ? "streaming-caret" : ""}`}
      >
        {content || (streaming ? "" : "(empty)")}
      </div>
      <div className="text-[10px] text-ink-100/40">
        {isUser ? "you" : isProactive ? "aiko · proactive" : "aiko"} ·{" "}
        {formatTime(createdAt)}
        {!isUser && reaction && reaction !== "neutral" ? ` · ${reaction}` : ""}
      </div>
    </li>
  );
}
