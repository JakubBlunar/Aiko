import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { api } from "../api";
import { backendBase } from "../desktop/runtime";
import { useMicCapture } from "../hooks/useMicCapture";
import { useAssistantStore } from "../store";
import type {
  AttachmentRef,
  ToolEvent,
  VoiceMode,
  WsClientCommand,
} from "../types";
import {
  SHARED_MOMENT_VIBES,
  TOUCH_GESTURE_LABELS,
  USER_REACTION_KINDS,
} from "../types";
import { ContextBadge } from "./ContextBadge";
import { MicButton } from "./MicButton";
import { TaskStrip } from "./TaskStrip";

interface ChatViewProps {
  send: (cmd: WsClientCommand) => void;
  sendBytes: (frame: Uint8Array) => void;
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
  // K58 directed-emotion shades.
  smug: "😏",
  pouty: "🥺",
  sulky: "😒",
  mischievous: "😈",
  wistful: "🌫️",
  neutral: "🌙",
};

/** Build the static URL for an uploaded attachment's stored file from
 * its ``Attachments:<uuid><ext>`` rel_path. Used for image thumbnails
 * in the composer + on user bubbles. */
function attachmentUrl(relPath: string): string {
  const storedName = relPath.includes(":")
    ? relPath.split(":").slice(1).join(":")
    : relPath;
  return `${backendBase().http}/attachment-files/${encodeURIComponent(storedName)}`;
}

const MAX_ATTACHMENTS = 8;

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

export function ChatView({ send, sendBytes }: ChatViewProps) {
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
  const clientId = useAssistantStore((s) => s.clientId);
  const voiceOwnerId = useAssistantStore((s) => s.voiceOwnerId);
  const remotelyOwned = Boolean(
    voiceOwnerId && clientId && voiceOwnerId !== clientId,
  );

  useMicCapture({ sendBytes });

  const [draft, setDraft] = useState("");
  // D2 Part B: attachments staged for the next message. Uploaded as
  // soon as they're picked (so the path exists when Aiko's workflow
  // resolves it); cleared on send / removal.
  const [pendingAttachments, setPendingAttachments] = useState<
    AttachmentRef[]
  >([]);
  const [uploadingCount, setUploadingCount] = useState(0);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Virtuoso virtualises the chat list so the DOM only ever holds the
  // bubbles currently on screen (plus a small overscan buffer). This
  // matters at two ends of the spectrum: long histories (50+ bubbles)
  // used to render every node on every store update; and the streaming
  // turn used to force re-layout of the entire list on every token.
  // ``followOutput`` carries the "stick-to-bottom unless the user
  // scrolled up" rule that the old hand-rolled scrollRef machinery
  // implemented; ``initialTopMostItemIndex`` lands on the latest
  // message on first paint, including after a session switch.
  const virtuosoRef = useRef<VirtuosoHandle | null>(null);
  // Snap the chat to the bottom on session change. The session-id
  // dependency triggers re-mount of the same Virtuoso instance with
  // the new ``messages`` array; we still want to land at the tail on
  // the very next render, regardless of where the previous session
  // left the scroll position.
  useEffect(() => {
    if (messages.length === 0) return;
    requestAnimationFrame(() => {
      virtuosoRef.current?.scrollToIndex({
        index: messages.length - 1,
        align: "end",
        behavior: "auto",
      });
    });
    // ``messages.length`` intentionally omitted: this effect is keyed
    // by the conversation, not by every token append.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionKey]);

  // Hide the transcript pill ~3s after we receive a final transcript.
  useEffect(() => {
    if (!lastTranscript) return;
    const id = window.setTimeout(() => setLastTranscript(""), 3000);
    return () => window.clearTimeout(id);
  }, [lastTranscript, setLastTranscript]);

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

  const handleFiles = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files);
      if (list.length === 0) return;
      setAttachError(null);
      for (const file of list) {
        // Respect the cap using the latest known count via the
        // functional check inside the uploader; a hard pre-check here
        // keeps the UX snappy for the common case.
        setPendingAttachments((prev) => {
          if (prev.length >= MAX_ATTACHMENTS) {
            setAttachError(`Up to ${MAX_ATTACHMENTS} attachments per message.`);
          }
          return prev;
        });
        setUploadingCount((n) => n + 1);
        try {
          const ref = await api.uploadAttachment(file);
          setPendingAttachments((prev) =>
            prev.length >= MAX_ATTACHMENTS ? prev : [...prev, ref],
          );
        } catch (err) {
          setAttachError(
            err instanceof Error ? err.message : `Couldn't attach ${file.name}`,
          );
        } finally {
          setUploadingCount((n) => Math.max(0, n - 1));
        }
      }
    },
    [],
  );

  const removeAttachment = useCallback((ref: AttachmentRef) => {
    setPendingAttachments((prev) =>
      prev.filter((a) => a.rel_path !== ref.rel_path),
    );
    // Best-effort delete of the stored bytes; ignore failures (a stale
    // file in the managed dir is harmless and gets reaped on cleanup).
    void api.deleteAttachment(ref.rel_path).catch(() => undefined);
  }, []);

  const handleSend = () => {
    const text = draft.trim();
    if (!text || turnInProgress || connection.status !== "connected") {
      return;
    }
    send(
      pendingAttachments.length > 0
        ? { type: "chat", text, attachments: pendingAttachments }
        : { type: "chat", text },
    );
    setDraft("");
    setPendingAttachments([]);
    setAttachError(null);
    // Sending a message is an explicit "I want to engage with the
    // latest content" gesture, so force-snap to the tail. This both
    // shows the user's bubble immediately and re-arms Virtuoso's
    // ``followOutput`` heuristic so Aiko's streaming reply keeps the
    // chat pinned to the bottom. Without this jump, a user who had
    // scrolled up to read history would type a message, see it land
    // off-screen, and then watch Aiko reply somewhere they can't see.
    // Two rAFs: first lets the new bubble + auto-grown textarea land,
    // second performs the scroll once final heights are measured.
    // Two rAFs: the first lets the store fan out the new user bubble
    // and the textarea collapse back to its single-line height; the
    // second performs the jump after the final layout is measured.
    // ``"LAST"`` resolves against Virtuoso's internal data length, so
    // we don't have to guess whether the user's bubble has landed in
    // ``messages`` yet from this closure's perspective.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        virtuosoRef.current?.scrollToIndex({
          index: "LAST",
          align: "end",
          behavior: "auto",
        });
      });
    });
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSend();
    }
  };

  const handlePaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData?.files ?? []);
    if (files.length > 0) {
      event.preventDefault();
      void handleFiles(files);
    }
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOver(false);
    const files = event.dataTransfer?.files;
    if (files && files.length > 0) {
      void handleFiles(files);
    }
  };

  const onFileInputChange = (
    event: React.ChangeEvent<HTMLInputElement>,
  ) => {
    if (event.target.files) {
      void handleFiles(event.target.files);
    }
    // Reset so picking the same file twice still fires onChange.
    event.target.value = "";
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

      {/* Chunk 14: live background-task strip. Renders only when at
          least one task is active or recently completed; the
          component returns null otherwise so the chat layout stays
          unchanged when nothing is running. */}
      <TaskStrip />

      <div className="flex min-h-0 flex-1 flex-col">
        {messages.length === 0 ? (
          <div className="flex-1 overflow-y-auto px-6 py-8">
            <EmptyState booting={connection.status !== "connected"} />
            {toolActivity.length > 0 ? (
              <ToolActivityStrip activity={toolActivity} />
            ) : null}
          </div>
        ) : (
          <Virtuoso
            ref={virtuosoRef}
            data={messages}
            // Stick to the bottom while the user is at the tail; freeze
            // the scroll position the moment they scroll up to read
            // history. ``"auto"`` is the same instant jump the old
            // ``scrollTop = scrollHeight`` did — switch to ``"smooth"``
            // here only if we ever want to animate it.
            followOutput={(isAtBottom) => (isAtBottom ? "auto" : false)}
            initialTopMostItemIndex={Math.max(0, messages.length - 1)}
            computeItemKey={(_index, msg) => msg.id}
            increaseViewportBy={{ top: 400, bottom: 600 }}
            className="flex-1"
            style={{ height: "100%" }}
            itemContent={(_index, msg) => (
              <div className="mx-auto max-w-3xl px-6 pb-4 first:pt-8">
                <MessageBubble {...msg} />
              </div>
            )}
            components={{
              Footer: () =>
                toolActivity.length > 0 ? (
                  <div className="px-6 pb-8">
                    <ToolActivityStrip activity={toolActivity} />
                  </div>
                ) : (
                  <div className="pb-8" />
                ),
            }}
          />
        )}
      </div>

      <div
        className={`border-t border-white/5 bg-white/[0.02] px-6 py-4 ${
          dragOver ? "ring-2 ring-inset ring-ink-400/60" : ""
        }`}
        onDragOver={(e) => {
          e.preventDefault();
          if (!dragOver) setDragOver(true);
        }}
        onDragLeave={(e) => {
          // Only clear when the pointer actually left the composer, not
          // when it crosses a child element.
          if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
          setDragOver(false);
        }}
        onDrop={handleDrop}
      >
        <div className="mx-auto max-w-3xl">
          <VoiceStrip
            voiceMode={voiceMode}
            audioLevel={audioLevel}
            lastTranscript={lastTranscript}
            currentPartial={currentPartial}
          />
          <AttachmentTray
            attachments={pendingAttachments}
            uploadingCount={uploadingCount}
            error={attachError}
            onRemove={removeAttachment}
          />
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,.txt,.md,.rst,.log,.json,.yaml,.yml,.toml,.ini,.cfg,.conf,.csv,.tsv,.py,.js,.ts,.tsx,.jsx,.html,.css,.xml,.sh,.bat,.ps1,.sql"
            className="hidden"
            onChange={onFileInputChange}
          />
          <div className="flex items-center gap-2">
            <MicButton
              voiceMode={voiceMode}
              audioLevel={audioLevel}
              connected={connection.status === "connected"}
              onClick={handleMicToggle}
              remotelyOwned={remotelyOwned}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={
                connection.status !== "connected" ||
                pendingAttachments.length >= MAX_ATTACHMENTS
              }
              className="flex h-12 w-12 shrink-0 items-center justify-center self-center rounded-xl border border-white/10 bg-black/30 text-ink-100/70 transition hover:bg-white/10 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-40"
              title="Attach an image or text file"
              aria-label="Attach a file"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-5 w-5"
                aria-hidden="true"
              >
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
              </svg>
            </button>
            <textarea
              ref={textareaRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
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

interface AttachmentTrayProps {
  attachments: AttachmentRef[];
  uploadingCount: number;
  error: string | null;
  onRemove: (ref: AttachmentRef) => void;
}

/** D2 Part B: staged attachments above the composer. Images show a
 * thumbnail; text files show a document chip. Each has a remove ✕. */
function AttachmentTray({
  attachments,
  uploadingCount,
  error,
  onRemove,
}: AttachmentTrayProps) {
  if (attachments.length === 0 && uploadingCount === 0 && !error) {
    return null;
  }
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2">
      {attachments.map((att) => (
        <div
          key={att.rel_path}
          className="group/att relative flex items-center gap-2 rounded-lg border border-white/10 bg-black/30 py-1 pl-1 pr-2"
          title={att.filename}
        >
          {att.kind === "image" ? (
            <img
              src={attachmentUrl(att.rel_path)}
              alt={att.filename}
              className="h-9 w-9 rounded object-cover"
            />
          ) : (
            <span className="flex h-9 w-9 items-center justify-center rounded bg-white/5 text-base">
              📄
            </span>
          )}
          <span className="max-w-[10rem] truncate text-xs text-ink-100/80">
            {att.filename}
          </span>
          <button
            type="button"
            onClick={() => onRemove(att)}
            className="ml-1 rounded px-1 text-xs text-ink-100/50 hover:bg-white/10 hover:text-ink-100"
            title="Remove attachment"
            aria-label={`Remove ${att.filename}`}
          >
            ✕
          </button>
        </div>
      ))}
      {uploadingCount > 0 ? (
        <span className="rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-xs text-ink-100/60">
          Uploading {uploadingCount}…
        </span>
      ) : null}
      {error ? (
        <span className="rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
          {error}
        </span>
      ) : null}
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
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  streaming?: boolean;
  reaction?: string;
  kind?: "proactive";
  /** Backend message id (when known) — gates the "Mark as moment" action. */
  backendId?: number;
  /** K31: touch kinds Aiko emitted on this turn (one badge per
   * entry, in order). Empty / undefined for plain bubbles. */
  gestures?: string[];
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
        className={`relative whitespace-pre-wrap rounded-2xl px-4 py-3 text-sm leading-relaxed shadow-md ${
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
            const meta = TOUCH_GESTURE_LABELS[g] ?? {
              label: g,
              emoji: "✨",
            };
            return (
              <span
                key={`${g}-${idx}`}
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

const MessageBubble = memo(MessageBubbleImpl);
