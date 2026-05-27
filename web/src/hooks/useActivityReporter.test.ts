import { describe, expect, it } from "vitest";

import { normaliseActiveAppName } from "./useActivityReporter";

/**
 * Pure-helper coverage for the activity reporter. The full hook is
 * tied to ``useEffect`` + Tauri runtime probing; we exercise it via
 * the manual smoke flow + the ``test_session_controller_activity``
 * suite on the backend. ``normaliseActiveAppName`` is the only piece
 * with non-trivial logic that's safe to unit test in pure Node.
 */

describe("normaliseActiveAppName", () => {
  it("returns null for null input", () => {
    expect(normaliseActiveAppName(null)).toBeNull();
  });

  it("trims whitespace", () => {
    expect(normaliseActiveAppName("  Firefox  ")).toBe("Firefox");
  });

  it("strips trailing .exe (Windows)", () => {
    expect(normaliseActiveAppName("Code.exe")).toBe("Code");
  });

  it("is case-insensitive when checking the .exe suffix", () => {
    expect(normaliseActiveAppName("Notepad.EXE")).toBe("Notepad");
  });

  it("filters our own app to null (Aiko)", () => {
    // The bundle's ``productName`` is "Aiko"; without the filter the
    // backend would otherwise be told "Jacob is in Aiko" which is
    // useless and confusing.
    expect(normaliseActiveAppName("Aiko")).toBeNull();
    expect(normaliseActiveAppName("aiko")).toBeNull();
    expect(normaliseActiveAppName("Aiko.exe")).toBeNull();
    expect(normaliseActiveAppName("aiko-desktop")).toBeNull();
  });

  it("returns null for empty / whitespace-only input", () => {
    expect(normaliseActiveAppName("")).toBeNull();
    expect(normaliseActiveAppName("   ")).toBeNull();
  });

  it("preserves arbitrary app names", () => {
    expect(normaliseActiveAppName("Visual Studio Code")).toBe(
      "Visual Studio Code",
    );
    expect(normaliseActiveAppName("Discord")).toBe("Discord");
  });
});
