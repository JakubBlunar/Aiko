import { useEffect, useRef, useState, type KeyboardEvent } from "react";

const COMPOSING_IDLE_MS = 2500;

interface PersonaInputProps {
  /** Wire to the same WS dispatch the main window uses; the parent
   * remains the single point that talks to the backend. */
  onSend(text: string): void;
  /** Typing edge for Live / the listening face. Never includes draft. */
  onComposing?(active: boolean): void;
  /** Disabled while the WS is reconnecting so we don't queue messages
   * the backend will never see. */
  connected: boolean;
  /** Disabled while a turn is streaming so a quick second Enter
   * doesn't double-send. */
  busy: boolean;
}

/**
 * Minimal one-line composer for the floating persona window. Enter sends,
 * Shift+Enter is intentionally not supported here — multi-line input
 * stays in the main window's full ``ChatView``. The persona window is
 * meant for short pings: "hey", "what's the time?", "remind me later".
 */
export function PersonaInput({
  onSend,
  onComposing,
  connected,
  busy,
}: PersonaInputProps) {
  const [draft, setDraft] = useState("");
  const idleRef = useRef<number | null>(null);
  const sentRef = useRef(false);

  const emitComposing = (active: boolean) => {
    if (sentRef.current === active) {
      return;
    }
    sentRef.current = active;
    onComposing?.(active);
  };

  const stopComposing = () => {
    if (idleRef.current !== null) {
      window.clearTimeout(idleRef.current);
      idleRef.current = null;
    }
    emitComposing(false);
  };

  const markComposing = () => {
    emitComposing(true);
    if (idleRef.current !== null) {
      window.clearTimeout(idleRef.current);
    }
    idleRef.current = window.setTimeout(() => {
      idleRef.current = null;
      emitComposing(false);
    }, COMPOSING_IDLE_MS);
  };

  useEffect(() => () => stopComposing(), []);

  const submit = () => {
    const text = draft.trim();
    if (!text || busy || !connected) {
      return;
    }
    onSend(text);
    setDraft("");
    stopComposing();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  };

  return (
    <input
      type="text"
      value={draft}
      onChange={(event) => {
        const next = event.target.value;
        setDraft(next);
        if (next) {
          markComposing();
        } else {
          stopComposing();
        }
      }}
      onBlur={stopComposing}
      onKeyDown={onKeyDown}
      disabled={!connected}
      placeholder={
        !connected ? "connecting..." : busy ? "thinking..." : "talk to aiko..."
      }
      aria-label="Send a message to Aiko"
      className="h-9 w-full flex-1 rounded-lg border border-white/15 bg-black/40 px-3 text-sm text-ink-100 placeholder:text-ink-100/40 focus:border-ink-400 focus:outline-none focus:ring-2 focus:ring-ink-500/40 disabled:cursor-not-allowed disabled:opacity-60"
    />
  );
}
