import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";
import type { ChatMessage } from "../../types";

/**
 * I6: "load older" pagination store contract.
 *
 *   * ``prependMessages`` puts older rows at the FRONT of the
 *     transcript and leaves the existing tail (and any streamingDraft)
 *     untouched.
 *   * It dedupes by ``backendId`` so a double-fire (or an overlapping
 *     page) can never duplicate bubbles.
 *   * ``historyHasMore`` / ``setHistoryHasMore`` gate the affordance.
 */

function reset(): void {
  useAssistantStore.getState().clearMessages();
  useAssistantStore.getState().setHistoryHasMore(false);
}

beforeEach(reset);

function msg(backendId: number, content: string): ChatMessage {
  return {
    id: `hist_${backendId}`,
    backendId,
    role: "user",
    content,
    createdAt: "2026-06-28T00:00:00Z",
  };
}

describe("prependMessages", () => {
  it("prepends older rows ahead of the current transcript", () => {
    useAssistantStore.getState().setMessages([msg(5, "e"), msg(6, "f")]);
    useAssistantStore.getState().prependMessages([msg(3, "c"), msg(4, "d")]);

    const contents = useAssistantStore.getState().messages.map((m) => m.content);
    expect(contents).toEqual(["c", "d", "e", "f"]);
  });

  it("dedupes rows whose backendId is already present", () => {
    useAssistantStore.getState().setMessages([msg(4, "d"), msg(5, "e")]);
    // Page overlaps on id 4 — it must not be duplicated.
    useAssistantStore.getState().prependMessages([msg(3, "c"), msg(4, "d")]);

    const contents = useAssistantStore.getState().messages.map((m) => m.content);
    expect(contents).toEqual(["c", "d", "e"]);
  });

  it("is a no-op for an empty page", () => {
    useAssistantStore.getState().setMessages([msg(1, "a")]);
    const before = useAssistantStore.getState().messages;
    useAssistantStore.getState().prependMessages([]);
    expect(useAssistantStore.getState().messages).toBe(before);
  });

  it("does not disturb an in-flight streaming draft", () => {
    useAssistantStore.getState().appendAssistantBubble();
    const draftBefore = useAssistantStore.getState().streamingDraft;
    useAssistantStore.getState().prependMessages([msg(1, "older")]);
    expect(useAssistantStore.getState().streamingDraft).toEqual(draftBefore);
    expect(useAssistantStore.getState().messages[0].content).toBe("older");
  });
});

describe("historyHasMore", () => {
  it("round-trips through the setter", () => {
    expect(useAssistantStore.getState().historyHasMore).toBe(false);
    useAssistantStore.getState().setHistoryHasMore(true);
    expect(useAssistantStore.getState().historyHasMore).toBe(true);
  });
});
