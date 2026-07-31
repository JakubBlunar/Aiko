/**
 * P37 — the streaming re-pin signature must be coarse but never collide.
 *
 * `ChatView` re-renders whenever this string changes, so the whole point
 * is for it to *stop* changing on most tokens. The failure mode on the
 * other side is a collision: if the draft id were bucketed away, a new
 * turn starting at length 0 would produce the previous turn's first
 * signature and the reply's first re-pin would silently not happen.
 */
import { describe, expect, it } from "vitest";

import { STREAM_REPIN_CHARS, streamRepinSignature } from "./streamRepin";

const draft = (id: string, length: number) => ({
  id,
  content: "x".repeat(length),
});

describe("streamRepinSignature", () => {
  it("is empty with no active draft", () => {
    expect(streamRepinSignature(null)).toBe("");
    expect(streamRepinSignature(undefined)).toBe("");
  });

  it("is stable across tokens inside one bucket", () => {
    const first = streamRepinSignature(draft("a", 1));
    for (let n = 2; n < STREAM_REPIN_CHARS; n += 1) {
      expect(streamRepinSignature(draft("a", n))).toBe(first);
    }
  });

  it("changes once the draft crosses a bucket boundary", () => {
    expect(streamRepinSignature(draft("a", STREAM_REPIN_CHARS - 1))).not.toBe(
      streamRepinSignature(draft("a", STREAM_REPIN_CHARS)),
    );
  });

  it("changes a bounded number of times over a long reply", () => {
    // A ~1200-char reply arriving one token at a time: the old exact-length
    // signature produced ~1200 distinct values and therefore ~1200
    // ChatView renders.
    const seen = new Set<string>();
    for (let n = 0; n <= 1200; n += 1) {
      seen.add(streamRepinSignature(draft("a", n)));
    }
    expect(seen.size).toBe(1200 / STREAM_REPIN_CHARS + 1);
  });

  it("distinguishes a new turn from the previous turn's first bucket", () => {
    // The collision that would break the first re-pin of every reply.
    expect(streamRepinSignature(draft("turn-2", 0))).not.toBe(
      streamRepinSignature(draft("turn-1", 0)),
    );
  });

  it("changes when the draft clears, so stream end still re-pins", () => {
    expect(streamRepinSignature(draft("a", 10))).not.toBe(
      streamRepinSignature(null),
    );
  });

  it("keeps the bucket within about one wrapped line of the text", () => {
    // The tail may lag by at most one bucket's worth of characters.
    expect(STREAM_REPIN_CHARS).toBeLessThanOrEqual(80);
  });
});
