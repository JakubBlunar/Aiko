/**
 * K31 + K32 wiring tests for ``MessageBubble``.
 *
 * The vitest config runs in a Node environment with no jsdom, so we
 * can't mount the component and walk its rendered tree. Instead we
 * lock in the contract by inspecting the source verbatim: that the
 * bubble renderer
 *
 *   1. pulls in the K31 + K32 helpers (``normalizeGesture`` +
 *      ``USER_REACTION_KINDS``) — a stale import would make every
 *      gesture badge fall back to the generic ``✨`` placeholder and
 *      every reaction tray button disappear,
 *   2. branches the gesture badge strip on ``gestureKinds.length`` so
 *      a bubble with no touch never renders the empty container,
 *   3. branches the reaction strip + hover tray on ``canReact`` (i.e.
 *      assistant + not streaming + has a persisted backendId) — that
 *      gating mirrors the mark-as-moment story so reactions never
 *      attach to a row the backend can't address,
 *   4. wires the hover tray button click through ``onToggleReaction``
 *      so the optimistic store update + REST round-trip both fire.
 *
 * These are deliberately *source* assertions, not behaviour tests —
 * they're the cheapest way to catch regressions like "someone renamed
 * the export and the bubble now silently no-ops on every reaction
 * click". A future jsdom move-over will replace them with proper
 * render assertions but the contracts stay the same.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const bubbleSource = readFileSync(resolve(here, "MessageBubble.tsx"), "utf-8");

describe("MessageBubble — K31 / B7 gesture badge wiring", () => {
  it("imports normalizeGesture from the shared types module", () => {
    expect(bubbleSource).toMatch(
      /import\s*\{[^}]*\bnormalizeGesture\b[^}]*\}\s*from\s*"@\/types"/s,
    );
  });

  it("threads the open-vocabulary gestures prop on BubbleProps", () => {
    // B7: each entry is a legacy string OR a {kind,label,emoji} descriptor.
    expect(bubbleSource).toMatch(
      /gestures\?:\s*\(string\s*\|\s*TouchGestureBadge\)\[\]/,
    );
  });

  it("branches the gesture badge strip on gestureKinds.length", () => {
    // Cheap proxy: the strip only renders when the array has entries.
    expect(bubbleSource).toMatch(/gestureKinds\.length\s*>\s*0/);
  });

  it("normalizes each gesture descriptor before rendering the badge", () => {
    // The bubble must accept a custom descriptor or a legacy string and
    // still render *something* (normalizeGesture owns the fallback chain).
    expect(bubbleSource).toMatch(/normalizeGesture\(g\)/);
    expect(bubbleSource).toMatch(/\{meta\.emoji\}/);
    expect(bubbleSource).toMatch(/Aiko \{meta\.label\}/);
  });
});

describe("normalizeGesture — B7 descriptor fallback chain", () => {
  it("resolves a built-in kind to its curated label + emoji", async () => {
    const { normalizeGesture } = await import("@/types");
    expect(normalizeGesture("hug")).toEqual({
      kind: "hug",
      label: "gave you a hug",
      emoji: "🫂",
    });
  });

  it("keeps a custom descriptor's own label + emoji", async () => {
    const { normalizeGesture } = await import("@/types");
    expect(
      normalizeGesture({
        kind: "fist_bump",
        label: "bumped your fist",
        emoji: "🤜",
      }),
    ).toEqual({ kind: "fist_bump", label: "bumped your fist", emoji: "🤜" });
  });

  it("humanizes the slug + uses ✨ for an unknown bare-string kind", async () => {
    const { normalizeGesture } = await import("@/types");
    expect(normalizeGesture("tug_sleeve")).toEqual({
      kind: "tug_sleeve",
      label: "tug sleeve",
      emoji: "✨",
    });
  });
});

describe("MessageBubble — K32 reaction strip wiring", () => {
  it("imports USER_REACTION_KINDS from the shared types module", () => {
    expect(bubbleSource).toMatch(
      /import\s*\{[^}]*\bUSER_REACTION_KINDS\b[^}]*\}\s*from\s*"@\/types"/s,
    );
  });

  it("threads the reactions prop on BubbleProps", () => {
    expect(bubbleSource).toMatch(
      /reactions\?:\s*Record<string,\s*number>/,
    );
  });

  it("gates the reaction strip on canReact (assistant + persisted + not streaming)", () => {
    // The boolean lands in the JSX as ``{canReact ? ... : null}``.
    expect(bubbleSource).toMatch(/const\s+canReact\s*=/);
    expect(bubbleSource).toMatch(/!isUser\s*&&\s*!streaming\s*&&\s*backendId\s*!=\s*null/);
    expect(bubbleSource).toMatch(/\{canReact\s*\?/);
  });

  it("walks USER_REACTION_KINDS for the hover tray buttons", () => {
    // The hover tray maps over the taxonomy and skips kinds already
    // present in the counter strip.
    expect(bubbleSource).toMatch(
      /USER_REACTION_KINDS\.map\(\(r\)\s*=>/,
    );
    expect(bubbleSource).toMatch(/\(reactions\?\.\[r\.kind\]\s*\?\?\s*0\)\s*>\s*0/);
  });

  it("dispatches onToggleReaction with the kind on click", () => {
    // The click handler is wired through a callback so the parent
    // owns the REST + optimistic store update path.
    expect(bubbleSource).toMatch(/onToggleReaction\(r\.kind\)/);
    expect(bubbleSource).toMatch(/onToggleReaction\(kindKey\)/);
  });

  it("renders the per-kind counter strip from reactionEntries", () => {
    expect(bubbleSource).toMatch(/const\s+reactionEntries\s*=\s*Object\.entries/);
    // Empty / zero counts are filtered.
    expect(bubbleSource).toMatch(/\(count\s*\?\?\s*0\)\s*>\s*0/);
  });

  it("shows count > 1 inline next to the emoji", () => {
    expect(bubbleSource).toMatch(/count\s*>\s*1/);
  });
});

describe("MessageBubble — taxonomy contract", () => {
  it("exports the same kind set both sides of the wire", async () => {
    // Statically inspect the types module to confirm the taxonomy
    // is the single source of truth -- the assertion catches a future
    // accidental shadowing in MessageBubble itself.
    const types = await import("@/types");
    expect(Array.isArray(types.USER_REACTION_KINDS)).toBe(true);
    expect(types.USER_REACTION_KINDS.length).toBeGreaterThanOrEqual(6);
    const kinds = new Set(types.USER_REACTION_KINDS.map((r) => r.kind));
    for (const expected of ["heart", "hug", "laugh", "thumbs", "rose", "surprise"]) {
      expect(kinds.has(expected)).toBe(true);
    }
    expect(typeof types.TOUCH_GESTURE_LABELS).toBe("object");
    expect(types.TOUCH_GESTURE_LABELS["wave"]).toBeDefined();
    expect(types.TOUCH_GESTURE_LABELS["hug"]).toBeDefined();
  });
});
