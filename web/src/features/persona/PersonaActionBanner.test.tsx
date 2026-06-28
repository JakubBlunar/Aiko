/**
 * Source-level wiring tests for the K31 + K32 ``PersonaActionBanner``.
 *
 * The vitest config runs in Node with no jsdom, so the component
 * itself can't be mounted; we instead lock in the contract by
 * inspecting the source (same approach used by
 * ``PersonaWindow.test.tsx``). The assertions cover the bits that
 * a renamed export / dropped subscription / mistuned timer could
 * break silently:
 *
 *   1. The component subscribes to the dedup counter
 *      ``avatarTouchAt`` (not to ``avatarTouch`` directly — that
 *      would trigger on identity changes only, not on every WS
 *      frame).
 *   2. Two paths read ``avatarTouch`` for the live payload: an
 *      enabled gate + the rendering branch.
 *   3. The 20s default timer is in place + a minimum of 1s clamps
 *      a misconfigured server-side value.
 *   4. The reactions API client is called and the store is
 *      optimistically updated through ``applyMessageReactions``.
 *   5. The component is gated on ``enabled`` and silently
 *      ``return null`` when off.
 *
 * A future jsdom move-over will turn these into render assertions
 * (banner shows after a store push, reaction click calls the
 * API exactly once, etc.) but the contracts will remain.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const bannerSource = readFileSync(
  resolve(here, "PersonaActionBanner.tsx"),
  "utf-8",
);

describe("PersonaActionBanner — subscription wiring", () => {
  it("subscribes to avatarTouchAt as the dedup trigger", () => {
    expect(bannerSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.avatarTouchAt\)/,
    );
  });

  it("also subscribes to avatarTouch for the payload + messages for the target id", () => {
    expect(bannerSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.avatarTouch\)/,
    );
    expect(bannerSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.messages\)/,
    );
  });

  it("subscribes to applyMessageReactions so optimistic updates fan out to the chat window", () => {
    expect(bannerSource).toMatch(
      /useAssistantStore\(\s*\(s\)\s*=>\s*s\.applyMessageReactions,?\s*\)/,
    );
  });

  it("scans messages backwards for the latest assistant bubble with a backendId", () => {
    // The for-loop walks from messages.length - 1 down to 0 and
    // skips system / user / streaming rows.
    expect(bannerSource).toMatch(
      /for\s*\(\s*let\s+i\s*=\s*messages\.length\s*-\s*1\s*;\s*i\s*>=\s*0/,
    );
    expect(bannerSource).toMatch(
      /m\.role\s*===\s*"assistant"\s*&&\s*m\.backendId\s*!=\s*null/,
    );
  });
});

describe("PersonaActionBanner — visibility lifetime", () => {
  it("defaults the banner duration to 20s", () => {
    expect(bannerSource).toMatch(/DEFAULT_DURATION_MS\s*=\s*20_000/);
  });

  it("uses setTimeout to auto-dismiss + clamps the floor at 1000ms", () => {
    expect(bannerSource).toMatch(/setTimeout\(/);
    expect(bannerSource).toMatch(/Math\.max\(1000,\s*durationMs\)/);
  });

  it("clears the prior timer when a fresh gesture lands (replace not stack)", () => {
    // A new gesture inside the visibility window must cancel the
    // previous setTimeout before arming a new one -- otherwise the
    // first banner's timer can hide the second banner early.
    expect(bannerSource).toMatch(
      /if\s*\(timerRef\.current\)\s*\{\s*clearTimeout\(timerRef\.current\);/,
    );
  });
});

describe("PersonaActionBanner — feature gating", () => {
  it("accepts an enabled prop and short-circuits when false", () => {
    expect(bannerSource).toMatch(/enabled\?:\s*boolean/);
    expect(bannerSource).toMatch(/if\s*\(!enabled\s*\|\|\s*!banner\)\s*\{\s*return\s+null;/);
  });

  it("hides the banner if enabled flips to false mid-life", () => {
    // The effect runs on every (avatarTouchAt | enabled | durationMs)
    // change and explicitly dismisses if the gate is off.
    expect(bannerSource).toMatch(
      /if\s*\(!enabled\)\s*\{[\s\S]{0,80}dismiss\(\)/,
    );
  });
});

describe("PersonaActionBanner — reaction round-trip", () => {
  it("calls api.addReaction / removeReaction with the message id + kind", () => {
    expect(bannerSource).toMatch(
      /api\.addReaction\(messageId,\s*kindClicked\)/,
    );
    expect(bannerSource).toMatch(
      /api\.removeReaction\(messageId,\s*kindClicked\)/,
    );
  });

  it("optimistically updates via applyMessageReactions before + after the REST call", () => {
    // The component sets the optimistic state, fires the REST call,
    // then reconciles with the server's authoritative response.
    const calls = [...bannerSource.matchAll(/applyMessageReactions\(/g)];
    // Two optimistic updates (add + remove paths) + two reconciles +
    // one rollback path on error.
    expect(calls.length).toBeGreaterThanOrEqual(5);
  });

  it("rolls back the optimistic write on a REST error and toasts a warning", () => {
    expect(bannerSource).toMatch(
      /catch[\s\S]{0,80}applyMessageReactions\(messageId,\s*current\)/,
    );
    expect(bannerSource).toMatch(/pushToast\(\s*"warning"/);
  });

  it("disables every reaction button while a request is in flight", () => {
    expect(bannerSource).toMatch(/reactBusyKind\s*!=\s*null/);
    expect(bannerSource).toMatch(/disabled=\{.*reactBusyKind\s*!=\s*null/s);
  });
});

describe("PersonaActionBanner — render contract", () => {
  it("renders one button per kind in USER_REACTION_KINDS", () => {
    expect(bannerSource).toMatch(
      /import\s*\{[\s\S]*?USER_REACTION_KINDS[\s\S]*?\}\s*from\s*"@\/types"/,
    );
    expect(bannerSource).toMatch(/USER_REACTION_KINDS\.map\(/);
  });

  it("renders the gesture label + emoji from TOUCH_GESTURE_LABELS with a payload fallback", () => {
    expect(bannerSource).toMatch(
      /TOUCH_GESTURE_LABELS\[banner\.payload\.kind\]/,
    );
    // Payload-provided ``label``/``emoji`` are the fallback when the
    // taxonomy doesn't include the kind (e.g. a future kind shipped
    // before the frontend bundle is rebuilt).
    expect(bannerSource).toMatch(/banner\.payload\.label\s*\|\|\s*banner\.payload\.kind/);
    expect(bannerSource).toMatch(/banner\.payload\.emoji\s*\|\|\s*"✨"/);
  });

  it("exposes a stable test-id for future jsdom-style queries", () => {
    expect(bannerSource).toMatch(/data-testid="persona-action-banner"/);
  });

  it("provides a Dismiss button that clears the timer + hides the banner", () => {
    expect(bannerSource).toMatch(/aria-label="Dismiss"/);
    expect(bannerSource).toMatch(/onClick=\{dismiss\}/);
  });
});
