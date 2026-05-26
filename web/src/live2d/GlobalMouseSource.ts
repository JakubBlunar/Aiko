/**
 * Global ``MouseSource`` for the Tauri desktop shell.
 *
 * Mirrors the constructor + ``MouseSource`` contract of
 * :class:`WindowMouseSource` so :file:`Live2DAvatar.tsx` can swap one
 * for the other on a single ``isTauri()`` branch. The difference is
 * the data path: instead of hooking DOM ``pointermove`` (which only
 * fires when the cursor is over our own webview), we poll Tauri's
 * ``cursorPosition()`` every frame and translate the result back into
 * window-relative logical pixels — exactly the coordinate space
 * :class:`GazeChannel` already expects.
 *
 * Why polling and not events? Tauri 2 doesn't expose a global
 * mouse-move event; the OS-level cursor stream sits behind a
 * synchronous query. One IPC per RAF tick is cheap (microseconds) and
 * far simpler than maintaining a Rust-side global hook.
 *
 * Cross-monitor behaviour: the OS cursor space is one continuous
 * virtual screen across every connected display, so the subtraction
 * ``cursor - innerPosition`` produces a window-relative offset that
 * may be negative or much larger than ``viewportWidth`` — that's
 * fine. ``GazeChannel``'s existing clamps saturate the gaze at
 * ``±0.7`` X / [-0.5, 0.7] Y, which reads as "Aiko is looking max in
 * that direction" without ever feeling out-of-range.
 */
import type { MouseSource } from "./AvatarEngine";
import type { MouseSnapshot } from "./types";
import {
  productionCursorApi,
  type CursorApi,
} from "../desktop/cursor";

export interface GlobalMouseSourceOptions {
  /** DOM element used to derive ``containerRect``. Same role as in
   * :class:`WindowMouseSource`: the gaze channel normalises the cursor
   * offset against the centre of this element. */
  container: HTMLElement;
  /** Monotonic clock for ``lastMoveAt``. Defaults to
   * ``performance.now``; tests inject a deterministic clock. */
  now?: () => number;
  /** Frame scheduler. Defaults to ``requestAnimationFrame`` /
   * ``cancelAnimationFrame`` on the global ``window``. Tests pass a
   * manual stepper. */
  scheduleFrame?: (cb: FrameRequestCallback) => number;
  cancelFrame?: (handle: number) => void;
  /** Cursor-position + window-geometry IO. Defaults to the production
   * ``@tauri-apps/api`` wrappers; tests inject a stub. */
  cursorApi?: CursorApi;
}

/** Internal cache keyed off the window's last-known geometry.
 * ``GlobalMouseSource`` re-fetches this only when Tauri tells us the
 * window moved or the scale factor changed; the per-frame hot path
 * never pays the geometry IPC cost. */
interface CachedGeometry {
  innerX: number;
  innerY: number;
  scaleFactor: number;
}

/** Default cursor-API stub used outside the Tauri webview, before the
 * first probe lands, etc. Returning ``null`` from these falls through
 * to the "no cursor data yet" branch in :class:`GazeChannel`. */
const DEFAULT_GEOMETRY: CachedGeometry = {
  innerX: 0,
  innerY: 0,
  scaleFactor: 1,
};

export class GlobalMouseSource implements MouseSource {
  private readonly _container: HTMLElement;
  private readonly _now: () => number;
  private readonly _scheduleFrame: (cb: FrameRequestCallback) => number;
  private readonly _cancelFrame: (handle: number) => void;
  private readonly _cursorApi: CursorApi;

  /** Window-relative cursor X / Y in CSS pixels. ``null`` until the
   * first successful poll lands. */
  private _x: number | null = null;
  private _y: number | null = null;
  /** Wall-clock-ish timestamp of the last poll that observed an
   * actual cursor *movement*. Sits still when the cursor is
   * stationary so :class:`GazeChannel`'s ``IDLE_BREAK_MS`` fires
   * correctly even though we're polling at 60Hz. */
  private _lastMoveAt = 0;
  private _windowFocused = true;
  private _geometry: CachedGeometry = DEFAULT_GEOMETRY;
  /** Whether the geometry cache has ever been refreshed. We treat the
   * default-zeros as "unknown" and skip the cursor translation
   * entirely until the first probe lands; otherwise the gaze briefly
   * tracks against ``(0, 0)`` and visibly snaps once the real
   * geometry arrives. */
  private _geometryReady = false;
  private _rafHandle: number | null = null;
  private _disposed = false;
  private _unlistenMoved: (() => void) | null = null;
  private _unlistenScale: (() => void) | null = null;

  constructor(options: GlobalMouseSourceOptions) {
    this._container = options.container;
    this._now =
      options.now ??
      (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
    this._scheduleFrame =
      options.scheduleFrame ??
      ((cb) =>
        typeof requestAnimationFrame !== "undefined"
          ? requestAnimationFrame(cb)
          : (setTimeout(() => cb(this._now()), 16) as unknown as number));
    this._cancelFrame =
      options.cancelFrame ??
      ((handle) => {
        if (typeof cancelAnimationFrame !== "undefined") {
          cancelAnimationFrame(handle);
        } else {
          clearTimeout(handle as unknown as ReturnType<typeof setTimeout>);
        }
      });
    this._cursorApi = options.cursorApi ?? productionCursorApi;
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
    if (this._disposed) {
      return () => undefined;
    }

    // Focus tracking still works through the DOM — Tauri webviews
    // dispatch ``focus`` / ``blur`` on ``window`` the same way a
    // browser tab does.
    let detachFocus: (() => void) | null = null;
    if (typeof window !== "undefined") {
      const onFocus = () => {
        this._windowFocused = true;
      };
      const onBlur = () => {
        this._windowFocused = false;
      };
      window.addEventListener("focus", onFocus);
      window.addEventListener("blur", onBlur);
      detachFocus = () => {
        window.removeEventListener("focus", onFocus);
        window.removeEventListener("blur", onBlur);
      };
    }

    // Prime the geometry cache + subscribe to refresh events. Both
    // are async; the RAF poll loop tolerates a missing cache (it
    // simply skips the cursor translation until the first probe
    // lands, see ``_pollOnce``).
    void this._refreshGeometry();
    void this._cursorApi
      .onWindowMoved(() => {
        void this._refreshGeometry();
      })
      .then((unlisten) => {
        if (this._disposed) {
          unlisten();
        } else {
          this._unlistenMoved = unlisten;
        }
      });
    void this._cursorApi
      .onScaleFactorChanged(() => {
        void this._refreshGeometry();
      })
      .then((unlisten) => {
        if (this._disposed) {
          unlisten();
        } else {
          this._unlistenScale = unlisten;
        }
      });

    // Start the RAF poll loop. The loop schedules itself; the
    // teardown function below cancels the next pending frame and
    // flips ``_disposed`` so any in-flight async work no-ops on
    // resolution.
    const tick = () => {
      if (this._disposed) {
        return;
      }
      void this._pollOnce();
      this._rafHandle = this._scheduleFrame(tick);
    };
    this._rafHandle = this._scheduleFrame(tick);

    return () => {
      this._disposed = true;
      if (this._rafHandle != null) {
        this._cancelFrame(this._rafHandle);
        this._rafHandle = null;
      }
      if (this._unlistenMoved) {
        this._unlistenMoved();
        this._unlistenMoved = null;
      }
      if (this._unlistenScale) {
        this._unlistenScale();
        this._unlistenScale = null;
      }
      if (detachFocus) {
        detachFocus();
      }
    };
  }

  // ── internals ────────────────────────────────────────────────────

  private async _refreshGeometry(): Promise<void> {
    const next = await this._cursorApi.getCurrentWindowGeometry();
    if (this._disposed || !next) {
      return;
    }
    this._geometry = next;
    this._geometryReady = true;
  }

  private async _pollOnce(): Promise<void> {
    const point = await this._cursorApi.getCursorPositionPhysical();
    if (this._disposed || !point || !this._geometryReady) {
      return;
    }
    const scale = this._geometry.scaleFactor || 1;
    // Convert physical → logical (CSS) pixels for both the cursor and
    // the window's inner top-left, then subtract. ``getBoundingClientRect``
    // is in CSS pixels so the resulting ``mouse.x / mouse.y`` lands in
    // the same coord space :class:`GazeChannel` reads.
    const nx = point.x / scale - this._geometry.innerX / scale;
    const ny = point.y / scale - this._geometry.innerY / scale;
    if (nx === this._x && ny === this._y) {
      // Cursor stationary between two polls. Don't refresh
      // ``lastMoveAt`` — it must reflect actual movement so the
      // gaze idle-break still fires when the user steps away from
      // the mouse.
      return;
    }
    this._x = nx;
    this._y = ny;
    this._lastMoveAt = this._now();
  }
}
