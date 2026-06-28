import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";

/**
 * B7: open-vocabulary touch-gesture descriptors on the store.
 *
 * ``appendGestureToCurrentTurn`` must:
 *   - stamp the streaming bubble when a draft is in flight, else the
 *     most recent assistant bubble (proactive / MCP send_touch),
 *   - accept either a bare ``kind`` string (legacy / convenience) or a
 *     full ``{kind,label,emoji}`` descriptor, always persisting a
 *     descriptor so a custom badge survives a reload,
 *   - dedup by kind regardless of stored shape.
 */

function reset(): void {
  useAssistantStore.getState().clearMessages();
}

beforeEach(reset);

describe("appendGestureToCurrentTurn — B7 descriptor flow", () => {
  it("stores a full descriptor on the streaming bubble for a custom gesture", () => {
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendAssistantToken("there, friend");

    useAssistantStore.getState().appendGestureToCurrentTurn({
      kind: "fist_bump",
      label: "bumped your fist",
      emoji: "🤜",
    });

    const msg = useAssistantStore.getState().messages[0];
    expect(msg.gestures).toEqual([
      { kind: "fist_bump", label: "bumped your fist", emoji: "🤜" },
    ]);
  });

  it("wraps a bare-string kind into a descriptor", () => {
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendGestureToCurrentTurn("hug");

    const msg = useAssistantStore.getState().messages[0];
    expect(msg.gestures).toEqual([{ kind: "hug" }]);
  });

  it("dedups by kind across string and descriptor shapes", () => {
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendGestureToCurrentTurn("hug");
    // Same kind again as a descriptor: should NOT add a second entry.
    useAssistantStore
      .getState()
      .appendGestureToCurrentTurn({ kind: "hug", emoji: "🫂" });

    const msg = useAssistantStore.getState().messages[0];
    expect(msg.gestures).toHaveLength(1);
  });

  it("stamps the most recent assistant bubble when no draft is active", () => {
    useAssistantStore.getState().setMessages([
      {
        id: "a1",
        role: "assistant",
        content: "hi",
        createdAt: "2026-01-01T00:00:00Z",
      },
    ]);

    useAssistantStore
      .getState()
      .appendGestureToCurrentTurn({ kind: "wave", label: "waved hi" });

    const msg = useAssistantStore.getState().messages[0];
    expect(msg.gestures).toEqual([{ kind: "wave", label: "waved hi" }]);
  });

  it("ignores an empty kind", () => {
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendGestureToCurrentTurn({ kind: "  " });

    const msg = useAssistantStore.getState().messages[0];
    expect(msg.gestures ?? []).toEqual([]);
  });
});
