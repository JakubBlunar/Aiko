import { useCallback, useEffect, useRef, useState } from "react";
import { useAssistantStore } from "@/store";
import {
  MIN_MOBILE_PERSONA_H,
  MIN_MOBILE_PERSONA_W,
  type MobilePersonaRect,
} from "@/store";
import { Live2DAvatar } from "./Live2DAvatar";

/** Over-zoom the rig so the floating window frames head + torso. The
 * Live2D fit logic top-anchors whenever the model overflows the
 * container vertically (see ``fitModelToContainer``), so any multiplier
 * comfortably above the window's aspect crops the legs and pins the
 * head to the top. */
const HEAD_TORSO_ZOOM = 1.9;

interface Gesture {
  mode: "drag" | "resize";
  startX: number;
  startY: number;
  orig: MobilePersonaRect;
}

function clampToViewport(rect: MobilePersonaRect): MobilePersonaRect {
  const vw = typeof window !== "undefined" ? window.innerWidth : 1024;
  const vh = typeof window !== "undefined" ? window.innerHeight : 768;
  const w = Math.min(Math.max(MIN_MOBILE_PERSONA_W, rect.w), vw);
  const h = Math.min(Math.max(MIN_MOBILE_PERSONA_H, rect.h), vh);
  const x = Math.min(Math.max(0, rect.x), Math.max(0, vw - w));
  const y = Math.min(Math.max(0, rect.y), Math.max(0, vh - h));
  return { x, y, w, h };
}

/**
 * Mobile floating persona window — an in-page, draggable + resizable box
 * showing Aiko's avatar (head + torso framing). Position + size live in
 * the store (persisted to ``localStorage``); this component keeps a
 * local copy for smooth pointer updates and commits to the store on
 * gesture end so we don't thrash ``localStorage`` on every move.
 *
 * Distinct from ``PersonaWindow`` (the desktop Tauri OS window) — this
 * one is pure DOM, positioned ``fixed`` over the chat, and only mounted
 * on the phone layout.
 */
export function FloatingPersona() {
  const storeRect = useAssistantStore((s) => s.mobilePersonaRect);
  const setStoreRect = useAssistantStore((s) => s.setMobilePersonaRect);
  const setVisible = useAssistantStore((s) => s.setMobilePersonaVisible);
  const avatar = useAssistantStore((s) => s.avatar);
  const ttsState = useAssistantStore((s) => s.ttsState);
  const voiceMode = useAssistantStore((s) => s.voiceMode);

  const [rect, setRect] = useState<MobilePersonaRect>(() =>
    clampToViewport(storeRect),
  );
  const rectRef = useRef(rect);
  rectRef.current = rect;
  const gestureRef = useRef<Gesture | null>(null);

  // Re-clamp into view when the viewport changes (rotation, keyboard
  // open/close) so the window can never end up fully offscreen.
  useEffect(() => {
    const onResize = () => {
      const next = clampToViewport(rectRef.current);
      setRect(next);
      setStoreRect(next);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [setStoreRect]);

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const g = gestureRef.current;
    if (!g) return;
    const dx = e.clientX - g.startX;
    const dy = e.clientY - g.startY;
    if (g.mode === "drag") {
      setRect(
        clampToViewport({
          ...g.orig,
          x: g.orig.x + dx,
          y: g.orig.y + dy,
        }),
      );
    } else {
      setRect(
        clampToViewport({
          ...g.orig,
          w: g.orig.w + dx,
          h: g.orig.h + dy,
        }),
      );
    }
  }, []);

  const endGesture = useCallback(
    (e: React.PointerEvent) => {
      if (!gestureRef.current) return;
      gestureRef.current = null;
      try {
        (e.currentTarget as Element).releasePointerCapture(e.pointerId);
      } catch {
        /* capture may already be gone */
      }
      // Commit the final geometry to the store (persists to localStorage).
      setStoreRect(rectRef.current);
    },
    [setStoreRect],
  );

  const beginGesture =
    (mode: Gesture["mode"]) => (e: React.PointerEvent) => {
      e.preventDefault();
      gestureRef.current = {
        mode,
        startX: e.clientX,
        startY: e.clientY,
        orig: rectRef.current,
      };
      try {
        (e.currentTarget as Element).setPointerCapture(e.pointerId);
      } catch {
        /* not all environments support capture */
      }
    };

  const statusLabel =
    voiceMode !== "off"
      ? voiceMode
      : ttsState === "speaking"
        ? "speaking"
        : "";

  return (
    <div
      className="fixed z-20 flex flex-col overflow-hidden rounded-xl border border-white/10 bg-black/40 shadow-2xl backdrop-blur-sm"
      style={{
        left: rect.x,
        top: rect.y,
        width: rect.w,
        height: rect.h,
        touchAction: "none",
      }}
    >
      {/* Drag handle */}
      <div
        onPointerDown={beginGesture("drag")}
        onPointerMove={onPointerMove}
        onPointerUp={endGesture}
        onPointerCancel={endGesture}
        className="flex shrink-0 cursor-grab items-center gap-1 bg-white/5 px-2 py-1 text-[10px] uppercase tracking-[0.2em] text-ink-100/50 active:cursor-grabbing"
        style={{ touchAction: "none" }}
      >
        <span className="font-medium">aiko</span>
        {statusLabel ? (
          <span className="text-ink-100/35">· {statusLabel}</span>
        ) : null}
        <button
          type="button"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => setVisible(false)}
          aria-label="Hide persona window"
          className="ml-auto flex h-4 w-4 items-center justify-center rounded text-ink-100/50 hover:bg-white/10 hover:text-ink-100"
        >
          ×
        </button>
      </div>

      {/* Avatar surface — head + torso framing via the zoom override. */}
      <div className="relative min-h-0 flex-1">
        {avatar && avatar.loaded ? (
          <Live2DAvatar manifest={avatar} scaleMultiplier={HEAD_TORSO_ZOOM} />
        ) : (
          <div className="flex h-full w-full items-center justify-center px-2 text-center text-[10px] text-ink-100/40">
            loading avatar…
          </div>
        )}
      </div>

      {/* Resize handle (bottom-right corner) */}
      <div
        onPointerDown={beginGesture("resize")}
        onPointerMove={onPointerMove}
        onPointerUp={endGesture}
        onPointerCancel={endGesture}
        aria-label="Resize persona window"
        className="absolute bottom-0 right-0 h-5 w-5 cursor-se-resize"
        style={{ touchAction: "none" }}
      >
        <svg
          viewBox="0 0 16 16"
          className="h-full w-full text-ink-100/40"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          aria-hidden="true"
        >
          <path d="M11 5 L5 11 M13 9 L9 13" />
        </svg>
      </div>
    </div>
  );
}
