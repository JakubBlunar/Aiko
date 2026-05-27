/**
 * Browser-side debug log bridge.
 *
 * Captures structured events (WS dispatch, avatar channel decisions,
 * settings changes, voice mode transitions) into a bounded ring buffer
 * and batches them out to ``POST /api/logs/ui`` so they interleave into
 * the same ``data/app.log`` file the backend writes. The goal: a single
 * file the user can share when reporting a bug so the assistant can see
 * the whole flow (cause + effect, backend + UI) without playing
 * forensic detective.
 *
 * Lifecycle
 * ---------
 * - Always installed; off by default. The Settings drawer's "Debug
 *   logging" toggle PATCHes ``logging.ui_log_enabled`` and the store
 *   subscriber calls :func:`debugLog.setEnabled` to flip the bit here.
 * - When disabled, :func:`debugLog.log` is a fast no-op. The ring
 *   buffer freezes at its last state so the user can still download
 *   whatever was captured up to the moment they turned it off.
 * - When enabled, every call appends to the ring (capped at
 *   ``RING_CAPACITY``) and enqueues an entry for the batcher. The
 *   batcher fires every ``FLUSH_INTERVAL_MS`` and ships up to
 *   ``MAX_BATCH`` entries per request; on HTTP errors it backs off
 *   exponentially so a transient 403 (toggle flipped off server-side)
 *   doesn't burn CPU. The buffer is left intact so a follow-up download
 *   still works.
 *
 * Console mirror
 * --------------
 * For power users staring at DevTools while reproducing a bug, setting
 * ``localStorage.alexiaConsoleMirror = "1"`` makes every accepted entry
 * also call ``console.debug`` with the same shape. Off by default so
 * the console stays quiet during normal use.
 */

import { backendBase } from "./desktop/runtime";

export interface UiLogEntry {
  /** ISO-8601 timestamp set at the moment the event was captured. The
   * backend echoes this on every emitted line so the rendering order
   * in ``app.log`` always reflects the producer's clock, not the
   * receiver's. */
  ts: string;
  /** Short identifier for what produced this event. Convention is
   * ``area`` or ``area.sub`` — e.g. ``ws``, ``channel.expression``,
   * ``settings.avatar``, ``voice``. The backend allow-list matches by
   * prefix so ``channel.expression`` is accepted whenever ``channel``
   * is on the list. */
  source: string;
  /** What happened. Short kebab-/camel-case label, e.g.
   * ``applyReaction``, ``pulseStart``, ``modeChanged``. */
  kind: string;
  /** Optional structured payload. Kept small; oversized blobs are
   * truncated server-side. */
  payload?: unknown;
}

const RING_CAPACITY = 2000;
const MAX_BATCH = 50;
const FLUSH_INTERVAL_MS = 500;
const MAX_BACKOFF_MS = 60_000;
const CONSOLE_MIRROR_KEY = "alexiaConsoleMirror";

let enabled = false;
const ring: UiLogEntry[] = [];
const queue: UiLogEntry[] = [];
let flushTimer: ReturnType<typeof setTimeout> | null = null;
let backoffMs = FLUSH_INTERVAL_MS;
let lastFlushAt = 0;
let inFlight = false;

function isoNow(): string {
  return new Date().toISOString();
}

function consoleMirrorEnabled(): boolean {
  try {
    return (
      typeof localStorage !== "undefined" &&
      localStorage.getItem(CONSOLE_MIRROR_KEY) === "1"
    );
  } catch {
    return false;
  }
}

function logUrl(): string {
  // Mirrors ``backendUrl`` in api.ts but inlined to avoid a circular
  // import (api.ts itself does not depend on log.ts; keep it that way).
  const base = backendBase().http;
  if (!base) return "/api/logs/ui";
  return `${base}/api/logs/ui`;
}

function pushToRing(entry: UiLogEntry): void {
  ring.push(entry);
  if (ring.length > RING_CAPACITY) {
    ring.splice(0, ring.length - RING_CAPACITY);
  }
}

function scheduleFlush(): void {
  if (flushTimer !== null) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    void flushNow();
  }, backoffMs);
}

async function flushNow(): Promise<void> {
  if (inFlight) {
    scheduleFlush();
    return;
  }
  if (!enabled || queue.length === 0) {
    return;
  }
  const batch = queue.splice(0, MAX_BATCH);
  inFlight = true;
  lastFlushAt = Date.now();
  try {
    const response = await fetch(logUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries: batch }),
    });
    if (response.status === 403) {
      // Backend disabled the feature; stop trying until the toggle
      // flips back. Re-queue nothing — once the user enables again
      // the next call to ``log()`` will reschedule with fresh data.
      queue.length = 0;
      backoffMs = MAX_BACKOFF_MS;
      return;
    }
    if (!response.ok) {
      // Transient error: re-queue the batch at the front and back off.
      queue.unshift(...batch);
      backoffMs = Math.min(MAX_BACKOFF_MS, Math.max(backoffMs * 2, 1000));
      return;
    }
    // Success — reset backoff. If more entries arrived while in
    // flight, schedule another flush immediately.
    backoffMs = FLUSH_INTERVAL_MS;
    if (queue.length > 0) {
      scheduleFlush();
    }
  } catch (err) {
    queue.unshift(...batch);
    backoffMs = Math.min(MAX_BACKOFF_MS, Math.max(backoffMs * 2, 1000));
    // Only warn once per backoff window so we don't spam the console
    // when the backend is down.
    if (typeof console !== "undefined" && backoffMs >= MAX_BACKOFF_MS) {
      console.warn("[debugLog] flush failed; backing off", err);
    }
  } finally {
    inFlight = false;
  }
}

export const debugLog = {
  /** Append a structured event. No-op while disabled (very cheap). */
  log(entry: Omit<UiLogEntry, "ts"> & { ts?: string }): void {
    if (!enabled) return;
    const full: UiLogEntry = {
      ts: entry.ts ?? isoNow(),
      source: entry.source,
      kind: entry.kind,
      payload: entry.payload,
    };
    pushToRing(full);
    queue.push(full);
    if (consoleMirrorEnabled() && typeof console !== "undefined") {
      console.debug("[ui]", full.source, full.kind, full.payload ?? "");
    }
    scheduleFlush();
  },

  /** Flip the master switch. Driven by the store subscriber that
   * watches ``loggingSettings.ui_log_enabled``. Turning OFF stops
   * future writes and drops the in-flight queue; the ring buffer is
   * preserved so a "Download" still works. */
  setEnabled(on: boolean): void {
    const next = Boolean(on);
    if (next === enabled) return;
    enabled = next;
    if (!enabled) {
      queue.length = 0;
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      backoffMs = FLUSH_INTERVAL_MS;
    } else if (queue.length > 0) {
      scheduleFlush();
    }
  },

  /** Whether the logger is currently accepting writes. */
  isEnabled(): boolean {
    return enabled;
  },

  /** Snapshot of the in-memory ring buffer. Safe to call any time;
   * returns a copy so callers can mutate without affecting the live
   * buffer. */
  snapshot(): UiLogEntry[] {
    return ring.slice();
  },

  /** Number of entries currently held in the ring. */
  size(): number {
    return ring.length;
  },

  /** Wall-clock ms of the last successful (or attempted) flush, or
   * 0 if nothing has been flushed yet. UI consumers display this as
   * "last flush 12s ago". */
  lastFlushAt(): number {
    return lastFlushAt;
  },

  /** Drop everything from the ring + queue. Used by the "Clear"
   * button in the drawer. */
  clear(): void {
    ring.length = 0;
    queue.length = 0;
  },

  /** Serialise the current buffer to a downloadable JSON file. The
   * filename includes an ISO timestamp so multiple captures from one
   * debug session don't clobber each other. Calling this when the
   * toggle is off still works — it returns whatever the ring captured
   * last. Safe to call in a non-browser context (e.g. unit tests);
   * the dom-touching parts are gated by typeof checks. */
  download(): void {
    const ts = new Date().toISOString().replace(/[:.]/g, "-");
    const blob =
      typeof Blob !== "undefined"
        ? new Blob([JSON.stringify(ring, null, 2)], {
            type: "application/json",
          })
        : null;
    if (blob === null || typeof document === "undefined") return;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `alexia-ui-log-${ts}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },

  /** Test hook. Forces a flush attempt synchronously (still async via
   * the network) and returns the promise so tests can await it. */
  flushNowForTests(): Promise<void> {
    return flushNow();
  },

  /** Test hook. Reset every piece of internal state. */
  __resetForTests(): void {
    enabled = false;
    ring.length = 0;
    queue.length = 0;
    if (flushTimer !== null) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    backoffMs = FLUSH_INTERVAL_MS;
    lastFlushAt = 0;
    inFlight = false;
  },
};

export const DEBUG_LOG_RING_CAPACITY = RING_CAPACITY;
export const DEBUG_LOG_MAX_BATCH = MAX_BATCH;
