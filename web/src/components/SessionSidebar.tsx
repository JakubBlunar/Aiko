import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useAssistantStore } from "../store";
import type { ChatMessage, SessionRow, WsClientCommand } from "../types";

interface SessionSidebarProps {
  send: (cmd: WsClientCommand) => void;
  onOpenSettings: () => void;
  /** Optional handler for toggling the detached persona window. Provided
   * only when the bundle is running inside a Tauri shell; rendered as a
   * second top-bar button next to "Settings". The label flips between
   * "Persona" / "Hide" based on ``personaWindowVisible`` so the user
   * always knows what the click will do. */
  onTogglePersona?: () => void;
  /** Whether the floating persona window is currently visible. Used to
   * style + label the toggle button. ``false`` when not in a Tauri
   * shell. */
  personaWindowVisible?: boolean;
  /** When ``true``, the sidebar collapses to a 56px icon rail. Toggled
   * via the chevron button in the expanded header (and the chevron in
   * the collapsed rail). The state is owned by the store so it
   * survives reloads and toolbar interactions outside this component. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

// ── Inline icons ────────────────────────────────────────────────────
//
// 16x16 stroke-only glyphs to avoid pulling in a 60kb icon library
// for ~5 sprites. ``currentColor`` so the colors flow from Tailwind
// utilities. Kept private to this file -- if a second component
// needs them, lift them into a shared module.

interface IconProps {
  className?: string;
}

function ChevronLeftIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10 3 L5 8 L10 13" />
    </svg>
  );
}

function ChevronRightIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M6 3 L11 8 L6 13" />
    </svg>
  );
}

function PlusIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 3 V13 M3 8 H13" />
    </svg>
  );
}

function PersonaIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="8" cy="6" r="2.6" />
      <path d="M3 13.5 C3.5 10.8 5.5 10 8 10 C10.5 10 12.5 10.8 13 13.5" />
    </svg>
  );
}

function SettingsIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.5 V3.4 M8 12.6 V14.5 M14.5 8 H12.6 M3.4 8 H1.5 M12.6 3.4 L11.3 4.7 M4.7 11.3 L3.4 12.6 M12.6 12.6 L11.3 11.3 M4.7 4.7 L3.4 3.4" />
    </svg>
  );
}

function formatRelative(iso: string | null): string {
  if (!iso) return "no activity";
  try {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const minutes = Math.round((now - then) / 60_000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return `${days}d ago`;
  } catch {
    return "";
  }
}

function shortId(sessionId: string): string {
  // Sessions look like "default:abc123ef"; show just the suffix.
  return sessionId.includes(":") ? sessionId.split(":", 2)[1] : sessionId;
}

export function SessionSidebar({
  send,
  onOpenSettings,
  onTogglePersona,
  personaWindowVisible = false,
  collapsed,
  onToggleCollapsed,
}: SessionSidebarProps) {
  const sessionKey = useAssistantStore((s) => s.sessionKey);
  const setMessages = useAssistantStore((s) => s.setMessages);
  const clearMessages = useAssistantStore((s) => s.clearMessages);
  const pushSystemMessage = useAssistantStore((s) => s.pushSystemMessage);

  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeKey, setActiveKey] = useState<string>("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.listSessions();
      setSessions(result.sessions);
      setActiveKey(result.active);
    } catch (err) {
      console.error("Failed to load sessions:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch + refresh on every session change broadcast.
  useEffect(() => {
    void refresh();
  }, [refresh, sessionKey]);

  // Whenever the active session key changes, hydrate the message list.
  useEffect(() => {
    if (!sessionKey) return;
    let cancelled = false;
    api
      .getMessages(sessionKey, 200)
      .then((rows) => {
        if (cancelled) return;
        const mapped: ChatMessage[] = rows.map((row, idx) => ({
          id: `hist_${idx}_${row.created_at}`,
          backendId: typeof row.id === "number" ? row.id : undefined,
          role: row.role === "user" ? "user" : row.role === "assistant" ? "assistant" : "system",
          content: row.content,
          createdAt: row.created_at,
          // K31/K32: restore the persisted gesture badges + reaction
          // counters so they survive a history reload / reconnect.
          ...(row.reactions ? { reactions: row.reactions } : {}),
          ...(row.gestures && row.gestures.length > 0
            ? { gestures: row.gestures }
            : {}),
          // D2 Part B: restore attachment chips/thumbnails on reload.
          ...(row.attachments && row.attachments.length > 0
            ? { attachments: row.attachments }
            : {}),
        }));
        setMessages(mapped);
      })
      .catch((err) => {
        console.error("Failed to load messages:", err);
        clearMessages();
        pushSystemMessage(`Failed to load history: ${String(err)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionKey, setMessages, clearMessages, pushSystemMessage]);

  const handleNew = () => {
    send({ type: "new_session" });
  };

  const handleSwitch = (row: SessionRow) => {
    if (row.session_id === activeKey) return;
    send({ type: "switch_session", session_id: row.session_id });
  };

  const handleDelete = async (row: SessionRow, evt: React.MouseEvent) => {
    evt.stopPropagation();
    if (!confirm(`Delete session ${shortId(row.session_id)}? This cannot be undone.`)) {
      return;
    }
    try {
      await api.deleteSession(row.session_id);
      if (row.session_id === activeKey) {
        send({ type: "new_session" });
      } else {
        await refresh();
      }
    } catch (err) {
      pushSystemMessage(`Failed to delete: ${String(err)}`);
    }
  };

  const handleClear = () => {
    if (!confirm("Clear all messages in the active session?")) return;
    send({ type: "clear" });
  };

  // ── Collapsed rail ────────────────────────────────────────────────
  //
  // 56px-wide icon rail with the four most useful actions stacked
  // vertically: expand, new session, persona toggle (Tauri only),
  // settings. Conversation list is hidden in this mode -- the user
  // can expand to switch sessions. The same DOM root keeps Tailwind
  // transitions trivial if we ever want to animate the collapse.
  if (collapsed) {
    return (
      <aside className="flex h-full w-14 shrink-0 flex-col items-center gap-2 border-r border-white/5 bg-black/30 py-3">
        <button
          type="button"
          onClick={onToggleCollapsed}
          title="Expand sidebar"
          aria-label="Expand sidebar"
          className="flex h-9 w-9 items-center justify-center rounded-md border border-white/10 text-ink-100/70 transition hover:border-ink-400 hover:text-ink-100"
        >
          <ChevronRightIcon className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={handleNew}
          title="New session"
          aria-label="New session"
          className="flex h-9 w-9 items-center justify-center rounded-md bg-ink-500 text-white transition hover:bg-ink-400"
        >
          <PlusIcon className="h-4 w-4" />
        </button>
        {onTogglePersona ? (
          <button
            type="button"
            onClick={onTogglePersona}
            title={
              personaWindowVisible
                ? "Hide detached persona window"
                : "Open detached persona window"
            }
            aria-label={
              personaWindowVisible
                ? "Hide detached persona window"
                : "Open detached persona window"
            }
            aria-pressed={personaWindowVisible}
            className={`flex h-9 w-9 items-center justify-center rounded-md border transition ${
              personaWindowVisible
                ? "border-pink-400/70 bg-pink-500/15 text-pink-100 hover:bg-pink-500/25"
                : "border-white/10 text-ink-100/70 hover:border-pink-400 hover:text-pink-100"
            }`}
          >
            <PersonaIcon className="h-4 w-4" />
          </button>
        ) : null}
        <div className="mt-auto">
          <button
            type="button"
            onClick={onOpenSettings}
            title="Settings"
            aria-label="Settings"
            className="flex h-9 w-9 items-center justify-center rounded-md border border-white/10 text-ink-100/70 transition hover:border-ink-400 hover:text-ink-100"
          >
            <SettingsIcon className="h-4 w-4" />
          </button>
        </div>
      </aside>
    );
  }

  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-white/5 bg-black/30">
      <div className="border-b border-white/5 px-4 py-4">
        <div className="flex items-center justify-between">
          <h1 className="text-base font-semibold tracking-tight text-ink-100">
            Aiko
          </h1>
          <div className="flex items-center gap-1">
            {onTogglePersona ? (
              <button
                type="button"
                onClick={onTogglePersona}
                title={
                  personaWindowVisible
                    ? "Hide detached persona window"
                    : "Open detached persona window"
                }
                aria-pressed={personaWindowVisible}
                className={`rounded-md border px-2 py-1 text-xs transition ${
                  personaWindowVisible
                    ? "border-pink-400/70 bg-pink-500/15 text-pink-100 hover:bg-pink-500/25"
                    : "border-white/10 text-ink-100/70 hover:border-pink-400 hover:text-pink-100"
                }`}
              >
                {personaWindowVisible ? "Hide" : "Persona"}
              </button>
            ) : null}
            <button
              type="button"
              onClick={onOpenSettings}
              className="rounded-md border border-white/10 px-2 py-1 text-xs text-ink-100/70 hover:border-ink-400 hover:text-ink-100"
            >
              Settings
            </button>
            <button
              type="button"
              onClick={onToggleCollapsed}
              title="Collapse sidebar"
              aria-label="Collapse sidebar"
              className="flex h-7 w-7 items-center justify-center rounded-md border border-white/10 text-ink-100/70 transition hover:border-ink-400 hover:text-ink-100"
            >
              <ChevronLeftIcon className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        <p className="mt-1 text-xs text-ink-100/50">Your AI friend</p>
      </div>

      <div className="flex items-center gap-2 px-4 py-3">
        <button
          type="button"
          onClick={handleNew}
          className="flex-1 rounded-md bg-ink-500 px-3 py-2 text-xs font-medium text-white hover:bg-ink-400"
        >
          + New session
        </button>
        <button
          type="button"
          onClick={handleClear}
          title="Clear active session messages"
          className="rounded-md border border-white/10 px-3 py-2 text-xs text-ink-100/70 hover:border-rose-400 hover:text-rose-200"
        >
          Clear
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-4">
        {loading && sessions.length === 0 ? (
          <div className="px-2 py-4 text-xs text-ink-100/50">Loading...</div>
        ) : sessions.length === 0 ? (
          <div className="px-2 py-4 text-xs text-ink-100/50">
            No sessions yet. Send a message to start one.
          </div>
        ) : (
          <ul className="flex flex-col gap-1">
            {sessions.map((row) => {
              const isActive = row.session_id === activeKey;
              return (
                <li key={row.session_id}>
                  <button
                    type="button"
                    onClick={() => handleSwitch(row)}
                    className={`group flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-xs transition ${
                      isActive
                        ? "bg-ink-700/60 text-ink-100"
                        : "text-ink-100/70 hover:bg-white/5 hover:text-ink-100"
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="truncate font-medium">
                        {shortId(row.session_id)}
                      </div>
                      <div className="text-[10px] text-ink-100/40">
                        {row.message_count} msgs ·{" "}
                        {formatRelative(row.last_activity)}
                      </div>
                    </div>
                    <span
                      role="button"
                      tabIndex={0}
                      onClick={(e) => void handleDelete(row, e)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          void handleDelete(row, e as unknown as React.MouseEvent);
                        }
                      }}
                      className="invisible ml-2 cursor-pointer rounded px-1 text-[10px] text-rose-300/80 hover:text-rose-200 group-hover:visible"
                      aria-label={`Delete session ${shortId(row.session_id)}`}
                    >
                      delete
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="border-t border-white/5 px-4 py-3 text-[10px] text-ink-100/40">
        <div>active: {shortId(sessionKey || activeKey || "main")}</div>
      </div>
    </aside>
  );
}
