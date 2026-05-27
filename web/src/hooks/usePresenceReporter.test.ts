import { describe, expect, it } from "vitest";

import { computeBrowserPresent } from "./usePresenceReporter";

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
