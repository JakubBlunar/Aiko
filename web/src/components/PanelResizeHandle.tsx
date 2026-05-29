import { useCallback, useRef } from "react";

interface PanelResizeHandleProps {
  /**
   * Accessible name for the separator. Mention which panel it
   * resizes so screen readers can disambiguate (e.g. "Resize avatar
   * panel").
   */
  ariaLabel: string;
  /**
   * Called on every pointer move while the handle is being dragged
   * with the signed delta in CSS pixels since the previous emission
   * (NOT cumulative). The parent decides how to apply it -- e.g. the
   * avatar panel subtracts it from its width because the handle sits
   * on its left edge.
   */
  onResize: (deltaX: number) => void;
  onResizeStart?: () => void;
  onResizeEnd?: () => void;
  /**
   * Keyboard step in CSS pixels. Defaults to ``8``; held with
   * ``Shift`` it scales by 4x for coarse resizing.
   */
  keyboardStep?: number;
}

/**
 * Vertical resize separator.
 *
 * Renders a 4px-wide rail that thickens on hover/focus to 6px. Drag
 * it with the mouse to resize the panel to its right; arrow keys
 * also work for keyboard accessibility (Left/Right ± step). Emits
 * raw pointer deltas via ``onResize`` so the same handle can drive
 * any panel that sits on either side of it.
 *
 * Why pointer-event drag instead of a CSS-only ``resize: horizontal``
 * trick: ``resize`` only works on block-level scroll containers and
 * pulls the bottom-right corner; we want a full-height edge handle
 * that doesn't move the panel content around. This is also why the
 * handle takes width via ``onResize`` callbacks rather than via a
 * controlled ``value``: the parent already owns the width state in
 * the Zustand store, and we don't want to round-trip every pointer
 * frame through ``set`` -> selector if the parent only needs deltas.
 */
export function PanelResizeHandle({
  ariaLabel,
  onResize,
  onResizeStart,
  onResizeEnd,
  keyboardStep = 8,
}: PanelResizeHandleProps) {
  // Track the previous pointer X in a ref so we can emit incremental
  // deltas without re-rendering on every move. Storing it in state
  // would queue a render per ``pointermove`` -- ~120 wasted renders
  // per second on a 120Hz mouse. The ref is also our "is dragging"
  // sentinel: ``null`` => idle, otherwise the last seen clientX.
  const lastXRef = useRef<number | null>(null);

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      // Only react to the primary pointer (left mouse / single finger).
      // Capture the pointer so we keep getting moves even when it
      // strays outside the handle bounds (very common during fast
      // drags).
      if (e.button !== 0) return;
      e.preventDefault();
      e.currentTarget.setPointerCapture(e.pointerId);
      lastXRef.current = e.clientX;
      onResizeStart?.();
    },
    [onResizeStart],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const last = lastXRef.current;
      if (last == null) return;
      const delta = e.clientX - last;
      if (delta === 0) return;
      lastXRef.current = e.clientX;
      onResize(delta);
    },
    [onResize],
  );

  const endDrag = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (lastXRef.current == null) return;
      lastXRef.current = null;
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        // The browser sometimes complains if capture was already
        // released (e.g. ``pointercancel`` after ``pointerup``); the
        // outcome is identical so we swallow it.
      }
      onResizeEnd?.();
    },
    [onResizeEnd],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const sign = e.key === "ArrowLeft" ? -1 : 1;
      const magnitude = keyboardStep * (e.shiftKey ? 4 : 1);
      onResize(sign * magnitude);
    },
    [keyboardStep, onResize],
  );

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={ariaLabel}
      tabIndex={0}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onKeyDown={handleKeyDown}
      className="group relative hidden h-full w-1 shrink-0 cursor-col-resize touch-none select-none border-l border-white/5 transition-colors hover:border-pink-400/60 focus-visible:border-pink-400 focus-visible:outline-none lg:block"
    >
      {/* Wider invisible hit-target so the user doesn't have to
          pixel-hunt the 1px border. Sits on top of the rail without
          affecting layout. */}
      <div className="absolute inset-y-0 -left-1 w-3" />
    </div>
  );
}
