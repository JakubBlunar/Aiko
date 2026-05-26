/**
 * Production ``MouseSource`` — wires real ``window`` mouse + focus
 * events. Created once per ``Live2DAvatar`` mount and handed to the
 * ``AvatarEngine`` via ``deps.mouseSource``.
 *
 * The source updates internal state on every ``pointermove`` /
 * ``focus`` / ``blur`` event but never schedules its own RAF — the
 * engine pulls a snapshot on each gaze tick. That keeps the read
 * deterministic and lets ``MouseSnapshot.lastMoveAt`` be measured in
 * the same monotonic clock the channels use.
 */
import type { MouseSource } from "./AvatarEngine";
import type { MouseSnapshot } from "./types";

export interface WindowMouseSourceOptions {
  /** DOM element used to derive ``containerRect``. The gaze channel
   * normalises the cursor offset against the centre of this element
   * so the avatar's "looking at the cursor" reads naturally. */
  container: HTMLElement;
  /** Monotonic clock for ``lastMoveAt``. Defaults to
   * ``performance.now``; tests inject ``FakeClock.now``. */
  now?: () => number;
}

export class WindowMouseSource implements MouseSource {
  private readonly _container: HTMLElement;
  private readonly _now: () => number;
  private _x: number | null = null;
  private _y: number | null = null;
  private _lastMoveAt = 0;
  private _windowFocused = true;

  constructor(options: WindowMouseSourceOptions) {
    this._container = options.container;
    this._now =
      options.now ??
      (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
    if (typeof document !== "undefined") {
      this._windowFocused = document.hasFocus();
    }
  }

  snapshot(): MouseSnapshot {
    const rect = this._container.getBoundingClientRect();
    return {
      x: this._x,
      y: this._y,
      lastMoveAt: this._lastMoveAt,
      windowFocused: this._windowFocused,
      containerRect: {
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
      },
      viewportWidth: typeof window !== "undefined" ? window.innerWidth : 0,
      viewportHeight: typeof window !== "undefined" ? window.innerHeight : 0,
    };
  }

  subscribe(): () => void {
    if (typeof window === "undefined") {
      return () => undefined;
    }
    const onMove = (e: PointerEvent) => {
      this._x = e.clientX;
      this._y = e.clientY;
      this._lastMoveAt = this._now();
    };
    const onFocus = () => {
      this._windowFocused = true;
    };
    const onBlur = () => {
      this._windowFocused = false;
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
    };
  }
}
