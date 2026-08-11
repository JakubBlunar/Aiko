import { useEffect } from "react";
import { api, mapRawMessages } from "@/api";
import { useAssistantStore } from "@/store";

/** Initial history page size on a session load. A full page means there
 * may be older messages to page back through (I6 "load older"). */
export const INITIAL_HISTORY_LIMIT = 200;

/**
 * Load the active session's transcript whenever the session key changes.
 *
 * This has to live somewhere that is mounted on **every** layout. It used
 * to sit inside `SessionSidebar`, which the desktop tree always renders
 * but the phone tree only mounts inside `MobileNavDrawer` — and that
 * returns `null` while closed. So on the PWA the socket would connect,
 * `hello` would set a perfectly good `sessionKey`, and the transcript
 * would simply never be fetched: the user got `ChatEmptyState` and no way
 * to tell it apart from a genuinely new conversation until they happened
 * to open the nav drawer. History is a property of the app, not of the
 * sidebar.
 */
export function useSessionHistory(): void {
  const sessionKey = useAssistantStore((s) => s.sessionKey);
  const setMessages = useAssistantStore((s) => s.setMessages);
  const setHistoryHasMore = useAssistantStore((s) => s.setHistoryHasMore);
  const clearMessages = useAssistantStore((s) => s.clearMessages);
  const pushSystemMessage = useAssistantStore((s) => s.pushSystemMessage);

  useEffect(() => {
    if (!sessionKey) return;
    let cancelled = false;
    api
      .getMessages(sessionKey, INITIAL_HISTORY_LIMIT)
      .then((rows) => {
        if (cancelled) return;
        setMessages(mapRawMessages(rows));
        setHistoryHasMore(rows.length >= INITIAL_HISTORY_LIMIT);
      })
      .catch((err) => {
        // Bail on a superseded request: without this a slow fetch for the
        // session we just left lands after the new one and blanks it.
        if (cancelled) return;
        console.error("Failed to load messages:", err);
        clearMessages();
        setHistoryHasMore(false);
        pushSystemMessage(`Failed to load history: ${String(err)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [
    sessionKey,
    setMessages,
    setHistoryHasMore,
    clearMessages,
    pushSystemMessage,
  ]);
}
