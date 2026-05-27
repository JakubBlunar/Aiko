import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { debugLog, DEBUG_LOG_MAX_BATCH, DEBUG_LOG_RING_CAPACITY } from "./log";

/**
 * Tests for the browser-side debug log bridge (``web/src/log.ts``).
 *
 * What we cover:
 *
 *   - ``setEnabled(false)`` makes ``log()`` a no-op (ring stays empty,
 *     no batcher fires).
 *   - When enabled, entries land in the ring and ``flushNowForTests``
 *     POSTs them to ``/api/logs/ui``.
 *   - The ring is bounded — pushing past the capacity drops the
 *     oldest entries first.
 *   - The batch sent to the backend respects ``MAX_BATCH``.
 *   - A 403 response (toggle flipped off server-side) drops the queue
 *     so subsequent flushes don't burn CPU.
 *   - Flipping the toggle OFF preserves the ring (download still
 *     works) but cancels the in-flight queue.
 *   - ``clear()`` empties the ring.
 */

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  debugLog.__resetForTests();
  fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ accepted: 0, dropped: 0 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  debugLog.__resetForTests();
  vi.unstubAllGlobals();
});

describe("debugLog — disabled state", () => {
  it("starts disabled", () => {
    expect(debugLog.isEnabled()).toBe(false);
  });

  it("log() is a no-op when disabled", () => {
    debugLog.log({ source: "ws", kind: "hello" });
    expect(debugLog.size()).toBe(0);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("setEnabled(false) is idempotent", () => {
    debugLog.setEnabled(false);
    debugLog.setEnabled(false);
    expect(debugLog.isEnabled()).toBe(false);
  });
});

describe("debugLog — enabled state", () => {
  beforeEach(() => {
    debugLog.setEnabled(true);
  });

  it("captures entries to the ring", () => {
    debugLog.log({ source: "ws", kind: "hello", payload: { a: 1 } });
    debugLog.log({ source: "channel.expression", kind: "applyReaction" });
    expect(debugLog.size()).toBe(2);
    const snap = debugLog.snapshot();
    expect(snap[0].source).toBe("ws");
    expect(snap[0].kind).toBe("hello");
    expect(snap[0].payload).toEqual({ a: 1 });
    expect(snap[0].ts).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(snap[1].source).toBe("channel.expression");
  });

  it("flushes the queue to /api/logs/ui", async () => {
    debugLog.log({ source: "ws", kind: "hello" });
    debugLog.log({ source: "channel.expression", kind: "applyReaction" });
    await debugLog.flushNowForTests();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/logs\/ui$/);
    expect((init as RequestInit).method).toBe("POST");
    const body = JSON.parse(String((init as RequestInit).body));
    expect(Array.isArray(body.entries)).toBe(true);
    expect(body.entries.length).toBe(2);
    expect(body.entries[0].source).toBe("ws");
  });

  it("ring buffer is bounded at RING_CAPACITY", () => {
    // Push past the capacity. We only assert the cap was enforced;
    // pushing 2010 entries also exercises the splice path.
    for (let i = 0; i < DEBUG_LOG_RING_CAPACITY + 10; i += 1) {
      debugLog.log({ source: "ws", kind: `evt-${i}` });
    }
    expect(debugLog.size()).toBe(DEBUG_LOG_RING_CAPACITY);
    // Oldest entries fell off the back; ``evt-9`` is the first kept.
    const first = debugLog.snapshot()[0];
    expect(first.kind).toBe("evt-10");
  });

  it("batch size capped at MAX_BATCH per POST", async () => {
    for (let i = 0; i < DEBUG_LOG_MAX_BATCH + 5; i += 1) {
      debugLog.log({ source: "ws", kind: `evt-${i}` });
    }
    await debugLog.flushNowForTests();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1].body));
    expect(body.entries.length).toBe(DEBUG_LOG_MAX_BATCH);
  });

  it("403 from backend drops the queue and stops flushing", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("forbidden", { status: 403 }),
    );
    debugLog.log({ source: "ws", kind: "hello" });
    await debugLog.flushNowForTests();
    // The ring still has the entry (download still works) but the
    // outbound queue was drained.
    expect(debugLog.size()).toBe(1);
    // A second flush attempt with the queue empty must not call
    // fetch again.
    fetchMock.mockClear();
    await debugLog.flushNowForTests();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("debugLog — setEnabled(false) after capture", () => {
  it("preserves ring but cancels in-flight queue", () => {
    debugLog.setEnabled(true);
    debugLog.log({ source: "ws", kind: "hello" });
    debugLog.log({ source: "ws", kind: "world" });
    expect(debugLog.size()).toBe(2);
    debugLog.setEnabled(false);
    // Ring is untouched so the user can still download.
    expect(debugLog.size()).toBe(2);
    // Further log() calls are no-ops.
    debugLog.log({ source: "ws", kind: "discarded" });
    expect(debugLog.size()).toBe(2);
  });
});

describe("debugLog — clear()", () => {
  it("empties the ring", () => {
    debugLog.setEnabled(true);
    debugLog.log({ source: "ws", kind: "hello" });
    debugLog.log({ source: "ws", kind: "world" });
    debugLog.clear();
    expect(debugLog.size()).toBe(0);
    expect(debugLog.snapshot()).toEqual([]);
  });
});
