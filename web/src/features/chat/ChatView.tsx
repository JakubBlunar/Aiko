import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { api, mapRawMessages } from "@/api";
import { useIsMobile } from "@/hooks/useIsMobile";
import { useMicCapture } from "@/hooks/useMicCapture";
import { useAssistantStore } from "@/store";
import { useTasksStore } from "@/stores/useTasksStore";
import type { AttachmentRef, WsClientCommand } from "@/types";
import { ContextBadge } from "./ContextBadge";
import { MicButton } from "@/features/voice/MicButton";
import { TaskStrip } from "@/features/tasks/TaskStrip";
import { AttachmentTray } from "./AttachmentTray";
import { ChatEmptyState } from "./ChatEmptyState";
import { ConnectionBadge } from "./ConnectionBadge";
import { LoadOlderHeader } from "./LoadOlderHeader";
import { MessageBubble } from "./MessageBubble";
import { ToolActivityStrip } from "./ToolActivityStrip";
import { VoiceStrip } from "./VoiceStrip";

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

const MAX_ATTACHMENTS = 8;

// I6: how many older messages to fetch per "Load older" click.
const OLDER_PAGE_SIZE = 100;
// react-virtuoso prepend pattern: ``firstItemIndex`` starts at a large
// baseline and is *decremented* by the number of prepended rows so the
// list keeps the viewport anchored on the same message instead of
// jumping when older history lands at the top.
const VIRTUOSO_START_INDEX = 1_000_000;

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
  // I6: "load older" pagination state.
  const historyHasMore = useAssistantStore((s) => s.historyHasMore);
  const setHistoryHasMore = useAssistantStore((s) => s.setHistoryHasMore);
  const prependMessages = useAssistantStore((s) => s.prependMessages);
  const [loadingOlder, setLoadingOlder] = useState(false);

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
  // Phones get a tighter composer: a compact mic, smaller file/send
  // hit-targets, and a single-line min height so the row doesn't eat
  // the screen on an iPhone SE.
  const isMobile = useIsMobile();
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
  // Re-pin support: Virtuoso's ``followOutput`` only fires on
  // ``messages`` count changes, so streaming tokens (stable
  // ``messages`` ref by P9), tool events (Footer), and task-strip
  // growth never re-trigger it. We track whether the user is parked
  // at the tail and grab the real scroller element so an effect can
  // nudge ``scrollTop = scrollHeight`` on those silent updates.
  const atBottomRef = useRef(true);
  const scrollerElRef = useRef<HTMLElement | null>(null);
  // I6: ``firstItemIndex`` for Virtuoso's prepend-stability. Tracked
  // alongside the session key it belongs to and derived inline, so a
  // session switch reads the baseline in the SAME render the new
  // ``sessionKey`` lands — no stale decremented value bleeding across
  // sessions (which would make Virtuoso miscount the prepend on the
  // first "load older" of the new session).
  const [firstItemState, setFirstItemState] = useState({
    key: sessionKey,
    firstItemIndex: VIRTUOSO_START_INDEX,
  });
  const firstItemIndex =
    firstItemState.key === sessionKey
      ? firstItemState.firstItemIndex
      : VIRTUOSO_START_INDEX;
  // Lightweight signatures: re-render ChatView (not the memoized
  // bubbles) when the streaming draft grows, or an active task's
  // status/progress/phase changes, so the re-pin effect below runs.
  const streamingSignature = useAssistantStore((s) =>
    s.streamingDraft
      ? `${s.streamingDraft.id}:${s.streamingDraft.content.length}`
      : "",
  );
  const activeTaskSignature = useTasksStore((s) => {
    const view = s.tasksView;
    return view.activeIds
      .map((id) => {
        const t = view.tasksById[id];
        return t ? `${id}:${t.status}:${t.progress ?? ""}:${t.phase ?? ""}` : `${id}`;
      })
      .join("|");
  });
  // Snap the chat to the bottom on session change. The session-id
  // dependency triggers re-mount of the same Virtuoso instance with
  // the new ``messages`` array; we still want to land at the tail on
  // the very next render, regardless of where the previous session
  // left the scroll position.
  useEffect(() => {
    if (messages.length === 0) return;
    requestAnimationFrame(() => {
      // ``"LAST"`` (not a numeric index) so this stays correct under
      // the I6 ``firstItemIndex`` shift — a numeric index would be read
      // in the shifted coordinate space after older pages are prepended.
      virtuosoRef.current?.scrollToIndex({
        index: "LAST",
        align: "end",
        behavior: "auto",
      });
    });
    // ``messages.length`` intentionally omitted: this effect is keyed
    // by the conversation, not by every token append.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionKey]);

  // Keep the latest line in view during the "silent" updates Virtuoso
  // doesn't observe — streaming tokens, tool-activity Footer growth,
  // and task-strip / phase changes. Only re-pin when the user is
  // already at the tail so scrolling up to read history still freezes
  // the view. Drive the actual scroller (not ``scrollToIndex('LAST')``)
  // because the streaming bubble and the Footer live below the last
  // virtualised item.
  useEffect(() => {
    if (!atBottomRef.current) return;
    const id = requestAnimationFrame(() => {
      const el = scrollerElRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(id);
  }, [streamingSignature, toolActivity.length, activeTaskSignature]);

  // I6: fetch the page of history immediately older than the oldest
  // message currently loaded and prepend it, keeping the viewport
  // anchored via ``firstItemIndex``. Keyset pagination (anchored on the
  // oldest backend id) so concurrent inserts can't shift the window.
  const loadOlder = useCallback(async () => {
    if (loadingOlder || !historyHasMore || !sessionKey) return;
    // Oldest loaded backend id (prepended pages live at the front, but a
    // leading system message may have no backendId — so scan for the min).
    let oldestId: number | undefined;
    for (const m of messages) {
      if (m.backendId != null && (oldestId == null || m.backendId < oldestId)) {
        oldestId = m.backendId;
      }
    }
    if (oldestId == null) {
      setHistoryHasMore(false);
      return;
    }
    setLoadingOlder(true);
    try {
      const rows = await api.getMessages(sessionKey, OLDER_PAGE_SIZE, oldestId);
      const mapped = mapRawMessages(rows);
      const known = new Set(
        messages
          .map((m) => m.backendId)
          .filter((id): id is number => id != null),
      );
      const fresh = mapped.filter(
        (m) => m.backendId == null || !known.has(m.backendId),
      );
      if (fresh.length > 0) {
        // Decrement the baseline by exactly the number of rows we're
        // prepending so Virtuoso holds the scroll position.
        setFirstItemState((prev) => ({
          key: sessionKey,
          firstItemIndex:
            (prev.key === sessionKey
              ? prev.firstItemIndex
              : VIRTUOSO_START_INDEX) - fresh.length,
        }));
        prependMessages(fresh);
      }
      // A short page (fewer than we asked for) means we've reached the
      // start of the conversation.
      setHistoryHasMore(rows.length >= OLDER_PAGE_SIZE);
    } catch (err) {
      console.error("Failed to load older messages:", err);
    } finally {
      setLoadingOlder(false);
    }
  }, [
    loadingOlder,
    historyHasMore,
    sessionKey,
    messages,
    prependMessages,
    setHistoryHasMore,
  ]);

  // Hide the transcript pill ~3s after we receive a final transcript.
  useEffect(() => {
    if (!lastTranscript) return;
    const id = window.setTimeout(() => setLastTranscript(""), 3000);
    return () => window.clearTimeout(id);
  }, [lastTranscript, setLastTranscript]);

  // Auto-grow the input textarea up to its max-height so the row doesn't
  // start out as a tall 2-line box but still expands naturally. On phones
  // the floor is a single 40px line and the cap is lower so a long draft
  // never swallows the viewport; clearing ``draft`` (e.g. after send)
  // re-runs this and collapses the box back to one line.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = isMobile ? 112 : 160; // mobile: ~4 lines; desktop: max-h-40
    const min = isMobile ? 40 : 48;
    const next = Math.min(max, Math.max(min, el.scrollHeight));
    el.style.height = `${next}px`;
  }, [draft, isMobile]);

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
    // Collapse the auto-grown box back to a single line immediately.
    // The ``[draft]`` effect also does this on the next render, but
    // clearing the inline height here removes any one-frame flash of the
    // old tall height (the symptom: the composer "staying large" after
    // submit on mobile).
    if (textareaRef.current) textareaRef.current.style.height = "";
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
    <div className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      {/* Header strip showing TTS state + reaction */}
      <div className="flex shrink-0 items-center justify-between border-b border-white/5 bg-white/[0.02] px-6 py-3">
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
            <ChatEmptyState booting={connection.status !== "connected"} />
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
            // Virtuoso's default "at bottom" tolerance is 4px, which mobile
            // momentum/rubber-band scrolling and fractional device-pixel
            // ratios routinely exceed — leaving the list a few px off the
            // bottom so ``isAtBottom`` reads false and ``followOutput`` stops
            // sticking on new messages. A roomier threshold on phones (and a
            // small bump on desktop) keeps the chat pinned to the tail.
            atBottomThreshold={isMobile ? 120 : 24}
            atBottomStateChange={(bottom) => {
              atBottomRef.current = bottom;
            }}
            scrollerRef={(el) => {
              scrollerElRef.current = (el as HTMLElement) ?? null;
            }}
            initialTopMostItemIndex={Math.max(0, messages.length - 1)}
            firstItemIndex={firstItemIndex}
            computeItemKey={(_index, msg) => msg.id}
            increaseViewportBy={{ top: 400, bottom: 600 }}
            className="flex-1"
            style={{ height: "100%" }}
            itemContent={(_index, msg) => (
              <div className="mx-auto max-w-3xl px-6 pb-4">
                <MessageBubble {...msg} />
              </div>
            )}
            components={{
              Header: () => (
                <LoadOlderHeader
                  hasMore={historyHasMore}
                  loading={loadingOlder}
                  onLoad={loadOlder}
                />
              ),
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
        className={`shrink-0 border-t border-white/5 bg-white/[0.02] px-6 py-4 ${
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
              size={isMobile ? "compact" : "default"}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={
                connection.status !== "connected" ||
                pendingAttachments.length >= MAX_ATTACHMENTS
              }
              className="flex h-9 w-9 shrink-0 items-center justify-center self-center rounded-lg border border-white/10 bg-black/30 text-ink-100/70 transition hover:bg-white/10 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-40 md:h-12 md:w-12 md:rounded-xl"
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
              className="h-10 min-h-[2.5rem] max-h-28 flex-1 resize-none self-center rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm leading-6 text-ink-100 placeholder:text-ink-100/40 focus:border-ink-400 focus:outline-none focus:ring-2 focus:ring-ink-500/40 disabled:opacity-60 md:h-12 md:min-h-[3rem] md:max-h-40 md:rounded-xl md:px-4 md:py-3"
            />
            {turnInProgress ? (
              <button
                type="button"
                onClick={() => send({ type: "stop" })}
                className="flex h-10 shrink-0 items-center justify-center rounded-lg bg-red-500/80 px-3 text-sm font-medium text-white transition hover:bg-red-500 md:h-12 md:min-w-[3rem] md:rounded-xl md:px-4"
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
                className="flex h-10 shrink-0 items-center justify-center rounded-lg bg-ink-500 px-3 text-sm font-medium text-white transition hover:bg-ink-400 disabled:cursor-not-allowed disabled:bg-white/10 disabled:text-white/40 md:h-12 md:min-w-[3rem] md:rounded-xl md:px-5"
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
