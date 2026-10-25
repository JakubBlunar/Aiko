import { useState } from "react";
import { api } from "@/api";
import { useAssistantStore } from "@/store";

export function MessageActions({ backendId, isUser }: {
  backendId: number;
  isUser: boolean;
}): React.JSX.Element {
  const sessionKey = useAssistantStore((s) => s.sessionKey);
  const turnInProgress = useAssistantStore((s) => s.turnInProgress);
  const connected = useAssistantStore((s) => s.connection.status === "connected");
  const latestUserId = useAssistantStore((s) => {
    for (let i = s.messages.length - 1; i >= 0; i -= 1) {
      if (s.messages[i].role === "user") return s.messages[i].backendId;
    }
    return undefined;
  });
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState<"delete" | "retry" | null>(null);
  const disabled = busy !== null || turnInProgress || !connected;

  async function act(action: "delete" | "retry"): Promise<void> {
    if (disabled) return;
    setBusy(action);
    try {
      if (action === "delete") {
        const result = await api.deleteChatMessage(sessionKey, backendId);
        const store = useAssistantStore.getState();
        if (store.sessionKey === result.session) store.removeMessages(result.message_ids);
      } else {
        await api.retryChatMessage(sessionKey, backendId);
      }
      setConfirmDelete(false);
    } catch (err) {
      useAssistantStore.getState().pushToast(
        "warning",
        `${action === "delete" ? "Could not delete message" : "Could not resend message"}: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
    } finally {
      setBusy(null);
    }
  }

  const buttonClass =
    "rounded px-1.5 py-0.5 text-[10px] hover:bg-white/10 disabled:opacity-40";

  return (
    <span className="ml-2 inline-flex flex-wrap items-center gap-1">
      {confirmDelete ? (
        <>
          <span>Delete this message?</span>
          <button type="button" className={buttonClass} disabled={disabled}
            onClick={() => void act("delete")}>
            {busy === "delete" ? "Deleting..." : "Delete"}
          </button>
          <button type="button" className={buttonClass} disabled={busy !== null}
            onClick={() => setConfirmDelete(false)}>Cancel</button>
        </>
      ) : (
        <button type="button" className={buttonClass} disabled={disabled}
          onClick={() => setConfirmDelete(true)} title="Delete this message from the conversation">
          Delete
        </button>
      )}
      {isUser && latestUserId === backendId ? (
        <button type="button" className={buttonClass} disabled={disabled}
          onClick={() => void act("retry")}
          title="Replace Aiko's reply to this message. Your message and attachments stay unchanged.">
          {busy === "retry" ? "Resending..." : "Resend latest"}
        </button>
      ) : null}
    </span>
  );
}
