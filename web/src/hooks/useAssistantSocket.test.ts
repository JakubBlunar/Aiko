import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import { useAssistantStore } from "../store";
import { finishTurnBubble } from "./finishTurnBubble";
import type { WsServerEvent } from "../types";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, "useAssistantSocket.ts"), "utf-8");

describe("turn completion", () => {
  type TurnDone = Extract<WsServerEvent, { type: "turn_done" }>;
  const saved = { type: "turn_done", metrics: { user_message_id: 7 },
    assistant_message_id: 8, delivery_id: "receipt-8" } as const;
  const cases: TurnDone[] = [
    { ...saved, session: "typed" }, saved,
    { type: "turn_done", metrics: {}, assistant_message_id: null, delivery_id: "" },
    { type: "turn_done", metrics: {}, delivery_id: "receipt-without-row" },
    { type: "turn_done", metrics: {} },
    { type: "turn_done", metrics: {}, assistant_message_id: 8 },
  ];
  it.each(cases)("finishes the bubble for %#", (evt) => {
    const store = useAssistantStore.getState();
    store.clearMessages();
    store.appendUserMessage("The ferry is here.");
    store.appendAssistantBubble();
    store.appendAssistantToken("Let's board.");
    const presentation = finishTurnBubble(store, evt);
    const { messages, streamingDraft } = useAssistantStore.getState();
    expect(streamingDraft).toBeNull();
    expect(messages[0].backendId ?? null).toEqual(evt.metrics.user_message_id ?? null);
    expect(messages[1]).toMatchObject({ role: "assistant", content: "Let's board.", streaming: false });
    expect(messages[1].backendId ?? null).toEqual(evt.assistant_message_id ?? null);
    expect(presentation).toEqual(evt.delivery_id === "receipt-8"
      ? { messageId: 8, deliveryId: "receipt-8" }
      : undefined);
  });
});

describe("sleep socket hydration", () => {
  it("routes scoped recovery events without making a second user bubble", () => {
    expect(source).toContain('case "user_message_saved":');
    expect(source).toContain("store.stampUserBackendId(evt.message_id, evt.text)");
    expect(source).toContain("store.removeMessages(evt.message_ids)");
    expect(source).toContain("store.replaceReplyAfter(evt.user_message_id)");
    expect(source).toContain("evt.session !== store.sessionKey");
    expect(source).toContain("store.streamingDraft === null");
  });
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
