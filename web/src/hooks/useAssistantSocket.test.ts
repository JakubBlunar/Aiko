import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, "useAssistantSocket.ts"), "utf-8");

describe("sleep socket hydration", () => {
  it("keeps text delivery bound to a visible message and the receiving connection", () => {
    expect(source).toContain("data-delivery-message");
    expect(source).toContain('document.visibilityState !== "visible"');
    expect(source).toContain("entry.intersectionRatio >= 1");
    expect(source).toContain("socketRef.current !== receivedOn");
    expect(source).toContain('state: "text_presented"');
    expect(source).toContain("setDeliveryListener(null)");
  });

  it("hydrates sleep from hello before incremental events arrive", () => {
    expect(source).toMatch(/if \(evt\.sleep\)/);
    expect(source).toMatch(/store\.setSleep\(evt\.sleep\)/);
  });

  it("applies sleep lifecycle broadcasts", () => {
    expect(source).toMatch(/case "sleep_state_changed":/);
    expect(source).toMatch(/store\.setSleep\(evt\.snapshot\)/);
  });

  it("hydrates live embodiment from hello and incremental events", () => {
    expect(source).toMatch(/evt\.live_embodiment/);
    expect(source).toMatch(/case "live_embodiment":/);
    expect(source).toMatch(/store\.setLiveEmbodiment/);
  });

  it("defers listening and the done chirp until playback_drained", () => {
    expect(source).toMatch(/type: "playback_drained"/);
    expect(source).toMatch(/setPlaybackDrainedListener/);
    expect(source).toMatch(/playDone\(\)/);
  });

  it("appends Live micro-utterances as aside bubbles", () => {
    expect(source).toMatch(/evt\.kind === "live_micro"/);
    expect(source).toMatch(/appendProactiveMessage\(evt\.content, evt\.message_id, evt\.kind\)/);
  });

  it("forwards activity_request to the desktop collector without awaiting", () => {
    expect(source).toMatch(/case "activity_request":/);
    expect(source).toMatch(/desktop\.requestActivitySnapshot\(evt\.request_id\)/);
  });
});
