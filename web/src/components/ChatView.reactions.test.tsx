/**
 * K31 + K32 wiring tests for ``ChatView``.
 *
 * The vitest config runs in a Node environment with no jsdom, so we
 * can't mount the component and walk its rendered tree. Instead we
 * lock in the contract by inspecting the source verbatim: that the
 * bubble renderer
 *
 *   1. pulls in the K31 + K32 taxonomies (``TOUCH_GESTURE_LABELS`` +
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
const chatViewSource = readFileSync(resolve(here, "ChatView.tsx"), "utf-8");

describe("ChatView — K31 gesture badge wiring", () => {
  it("imports TOUCH_GESTURE_LABELS from the shared types module", () => {
    expect(chatViewSource).toMatch(
      /import\s*\{[^}]*\bTOUCH_GESTURE_LABELS\b[^}]*\}\s*from\s*"\.\.\/types"/s,
    );
  });

  it("threads the gestures prop on BubbleProps", () => {
    expect(chatViewSource).toMatch(/gestures\?:\s*string\[\]/);
  });

  it("branches the gesture badge strip on gestureKinds.length", () => {
    // Cheap proxy: the strip only renders when the array has entries.
    expect(chatViewSource).toMatch(/gestureKinds\.length\s*>\s*0/);
  });

  it("reads gesture metadata through TOUCH_GESTURE_LABELS with a fallback", () => {
    // The bubble must accept an unknown kind and still render *something*.
    expect(chatViewSource).toMatch(/TOUCH_GESTURE_LABELS\[g\]/);
    expect(chatViewSource).toMatch(/label:\s*g,/);
    expect(chatViewSource).toMatch(/emoji:\s*"✨"/);
  });
});

describe("ChatView — K32 reaction strip wiring", () => {
  it("imports USER_REACTION_KINDS from the shared types module", () => {
    expect(chatViewSource).toMatch(
      /import\s*\{[^}]*\bUSER_REACTION_KINDS\b[^}]*\}\s*from\s*"\.\.\/types"/s,
    );
  });

  it("threads the reactions prop on BubbleProps", () => {
    expect(chatViewSource).toMatch(
      /reactions\?:\s*Record<string,\s*number>/,
    );
  });

  it("gates the reaction strip on canReact (assistant + persisted + not streaming)", () => {
    // The boolean lands in the JSX as ``{canReact ? ... : null}``.
    expect(chatViewSource).toMatch(/const\s+canReact\s*=/);
    expect(chatViewSource).toMatch(/!isUser\s*&&\s*!streaming\s*&&\s*backendId\s*!=\s*null/);
    expect(chatViewSource).toMatch(/\{canReact\s*\?/);
  });

  it("walks USER_REACTION_KINDS for the hover tray buttons", () => {
    // The hover tray maps over the taxonomy and skips kinds already
    // present in the counter strip.
    expect(chatViewSource).toMatch(
      /USER_REACTION_KINDS\.map\(\(r\)\s*=>/,
    );
    expect(chatViewSource).toMatch(/\(reactions\?\.\[r\.kind\]\s*\?\?\s*0\)\s*>\s*0/);
  });

  it("dispatches onToggleReaction with the kind on click", () => {
    // The click handler is wired through a callback so the parent
    // owns the REST + optimistic store update path.
    expect(chatViewSource).toMatch(/onToggleReaction\(r\.kind\)/);
    expect(chatViewSource).toMatch(/onToggleReaction\(kindKey\)/);
  });

  it("renders the per-kind counter strip from reactionEntries", () => {
    expect(chatViewSource).toMatch(/const\s+reactionEntries\s*=\s*Object\.entries/);
    // Empty / zero counts are filtered.
    expect(chatViewSource).toMatch(/\(count\s*\?\?\s*0\)\s*>\s*0/);
  });

  it("shows count > 1 inline next to the emoji", () => {
    expect(chatViewSource).toMatch(/count\s*>\s*1/);
  });
});

describe("ChatView — taxonomy contract", () => {
  it("exports the same kind set both sides of the wire", async () => {
    // Statically inspect the types module to confirm the taxonomy
    // is the single source of truth -- the assertion catches a future
    // accidental shadowing in ChatView itself.
    const types = await import("../types");
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
