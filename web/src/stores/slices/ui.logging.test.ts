import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";

/**
 * Tests for the ``loggingSettings`` slice of the Zustand store.
 *
 * The Settings drawer's "Debug logging" toggle and the WS
 * ``logging_settings_changed`` broadcast both flow through this slice,
 * which in turn drives :func:`debugLog.setEnabled` on the logger
 * singleton. Coverage here lives at the data layer; the
 * subscriber wiring sits in :file:`App.tsx` /
 * :file:`hooks/useAssistantSocket.ts`.
 */

const RESET = {
  loggingSettings: {
    ui_log_enabled: false,
    ui_log_categories: ["ws", "channel", "settings", "voice"],
    ui_log_max_batch: 50,
    ui_log_max_payload_bytes: 2048,
  },
};

describe("loggingSettings slice", () => {
  beforeEach(() => {
    useAssistantStore.setState(RESET);
  });

  it("starts disabled (privacy-respecting default)", () => {
    expect(
      useAssistantStore.getState().loggingSettings.ui_log_enabled,
    ).toBe(false);
  });

  it("setLoggingSettings replaces the whole block with defaults applied", () => {
    useAssistantStore.getState().setLoggingSettings({
      ui_log_enabled: true,
      ui_log_categories: ["ws"],
      ui_log_max_batch: 10,
      ui_log_max_payload_bytes: 512,
    });
    const next = useAssistantStore.getState().loggingSettings;
    expect(next.ui_log_enabled).toBe(true);
    expect(next.ui_log_categories).toEqual(["ws"]);
    expect(next.ui_log_max_batch).toBe(10);
    expect(next.ui_log_max_payload_bytes).toBe(512);
  });

  it("patchLoggingSettings keeps unspecified keys at their previous value", () => {
    useAssistantStore.getState().patchLoggingSettings({ ui_log_enabled: true });
    const next = useAssistantStore.getState().loggingSettings;
    expect(next.ui_log_enabled).toBe(true);
    // Bounds keys untouched.
    expect(next.ui_log_max_batch).toBe(50);
    expect(next.ui_log_categories).toEqual([
      "ws",
      "channel",
      "settings",
      "voice",
    ]);
  });
});
