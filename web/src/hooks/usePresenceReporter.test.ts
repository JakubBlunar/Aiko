import { describe, expect, it } from "vitest";

import { computeBrowserPresent, foldPresence } from "./usePresenceReporter";

/**
 * Pure-helper coverage for the presence reporter's browser-side
 * visibility check. The hook itself wires DOM event listeners and
 * Tauri webview-window listeners and is exercised end-to-end via
 * manual smoke. The pure boolean derivation is what we lock in here
 * so a regression in either the visibilityState OR the hasFocus
 * branch surfaces with a quick test.
 */

function makeDoc(
  partial: Partial<{
    visibilityState: DocumentVisibilityState;
    focused: boolean;
  }> = {},
): Pick<Document, "visibilityState" | "hasFocus"> {
  const visibilityState =
    partial.visibilityState ?? "visible";
  const focused = partial.focused ?? true;
  return {
    visibilityState,
    hasFocus: () => focused,
  };
}

describe("computeBrowserPresent", () => {
  it("returns true when document is missing (SSR pass)", () => {
    expect(computeBrowserPresent(null)).toBe(true);
  });

  it("returns true when visible AND focused", () => {
    expect(
      computeBrowserPresent(makeDoc({ visibilityState: "visible", focused: true })),
    ).toBe(true);
  });

  it("returns false when tab is hidden", () => {
    expect(
      computeBrowserPresent(makeDoc({ visibilityState: "hidden" })),
    ).toBe(false);
  });

  it("returns false when tab is visible but window is not focused", () => {
    // Covers the alt-tab-to-another-app case: tab visibility is
    // still ``visible`` (the tab itself wasn't switched) but focus
    // moved to a different OS window.
    expect(
      computeBrowserPresent(
        makeDoc({ visibilityState: "visible", focused: false }),
      ),
    ).toBe(false);
  });

  it("treats a missing hasFocus as present", () => {
    // Older browsers / non-standard environments may not implement
    // ``hasFocus``. We default to "present" so a missing API doesn't
    // accidentally silence the user.
    const doc = {
      visibilityState: "visible" as const,
    } as unknown as Pick<Document, "visibilityState" | "hasFocus">;
    expect(computeBrowserPresent(doc)).toBe(true);
  });
});

describe("foldPresence", () => {
  it("ignores Tauri signals in the browser (visible == browserPresent)", () => {
    expect(
      foldPresence({
        isTauri: false,
        tauriFocused: false,
        tauriWindowVisible: false,
        browserPresent: true,
      }),
    ).toBe(true);
    expect(
      foldPresence({
        isTauri: false,
        tauriFocused: true,
        tauriWindowVisible: true,
        browserPresent: false,
      }),
    ).toBe(false);
  });

  it("is present in Tauri only when focused AND shown AND browser-present", () => {
    expect(
      foldPresence({
        isTauri: true,
        tauriFocused: true,
        tauriWindowVisible: true,
        browserPresent: true,
      }),
    ).toBe(true);
  });

  it("reports NOT present when the window is hidden even if focus/document say otherwise", () => {
    // The core of the silent-TTS fix: a persona window that was
    // ``hide()``-d to the tray can keep reporting focus + a
    // ``visible`` document on Windows. Folding the authoritative OS
    // window-visibility in forces ``false`` so it drops out of the
    // audio-owner election's visible pool.
    expect(
      foldPresence({
        isTauri: true,
        tauriFocused: true,
        tauriWindowVisible: false,
        browserPresent: true,
      }),
    ).toBe(false);
  });

  it("reports NOT present when the window is shown but blurred (alt-tab)", () => {
    expect(
      foldPresence({
        isTauri: true,
        tauriFocused: false,
        tauriWindowVisible: true,
        browserPresent: true,
      }),
    ).toBe(false);
  });
});
