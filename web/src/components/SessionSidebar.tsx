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
          role: row.role === "user" ? "user" : row.role === "assistant" ? "assistant" : "system",
          content: row.content,
          createdAt: row.created_at,
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
