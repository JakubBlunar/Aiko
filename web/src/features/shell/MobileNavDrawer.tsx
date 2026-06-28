import { useEffect } from "react";
import { SessionSidebar } from "@/features/sessions/SessionSidebar";
import type { WsClientCommand } from "@/types";

interface MobileNavDrawerProps {
  open: boolean;
  onClose: () => void;
  send: (cmd: WsClientCommand) => void;
  onOpenSettings: () => void;
}

/**
 * Phone-only left navigation drawer. Slides the existing
 * ``SessionSidebar`` (chat history + new chat + settings) in over the
 * chat with a tap-to-dismiss backdrop. Auto-closes after any navigation
 * action via ``SessionSidebar``'s ``onAfterNavigate`` hook so the user
 * lands straight back on the conversation.
 */
export function MobileNavDrawer({
  open,
  onClose,
  send,
  onOpenSettings,
}: MobileNavDrawerProps) {
  // Close on Escape for keyboard / external-keyboard users.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40 flex">
      <div
        className="flex-1 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        role="presentation"
        // The sidebar sits on the LEFT, backdrop on the right, so put the
        // backdrop after the panel in the visual order via flex order.
        style={{ order: 2 }}
      />
      {/* SessionSidebar's root is ``bg-black/30`` (semi-transparent — it
          sits against the solid app gradient on desktop). In this overlay
          the chat shows through it, so the panel wrapper carries an opaque
          backdrop matching the top of the body gradient. */}
      <div
        style={{ order: 1, background: "#0f0a1f" }}
        className="h-full shadow-2xl"
      >
        <SessionSidebar
          send={send}
          onOpenSettings={onOpenSettings}
          collapsed={false}
          onToggleCollapsed={() => {}}
          onAfterNavigate={onClose}
          hideCollapseToggle
        />
      </div>
    </div>
  );
}
