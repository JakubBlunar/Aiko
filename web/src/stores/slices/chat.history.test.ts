import { beforeEach, describe, expect, it } from "vitest";
import { useAssistantStore } from "../../store";

beforeEach(() => useAssistantStore.getState().clearMessages());

describe("message recovery", () => {
  it("stamps the matching queued user bubble and ignores duplicate saves", () => {
    const store = useAssistantStore.getState();
    store.appendUserMessage("first");
    store.appendUserMessage("second");
    store.stampUserBackendId(12, "first");
    store.stampUserBackendId(12, "first");
    store.stampUserBackendId(13, "second");
    expect(useAssistantStore.getState().messages.map((m) => m.backendId)).toEqual([12, 13]);
  });

  it("handles identical queued messages without stamping the wrong saved row twice", () => {
    const store = useAssistantStore.getState();
    store.appendUserMessage("again");
    store.appendUserMessage("again");
    store.stampUserBackendId(1, "again");
    store.stampUserBackendId(1);
    store.stampUserBackendId(2, "again");
    expect(useAssistantStore.getState().messages.map((m) => m.backendId)).toEqual([1, 2]);
  });

  it("deletes only selected messages and repeated broadcasts are idempotent", () => {
    const store = useAssistantStore.getState();
    store.setMessages([
      { id: "u", backendId: 1, role: "user", content: "question", createdAt: "old" },
      { id: "a", backendId: 2, role: "assistant", content: "bad", createdAt: "old" },
      { id: "b", backendId: 3, role: "assistant", content: "keep", createdAt: "old" },
    ]);
    store.removeMessages([2]);
    const messages = useAssistantStore.getState().messages;
    store.removeMessages([2]);
    expect(useAssistantStore.getState().messages).toBe(messages);
    expect(messages.map((m) => m.backendId)).toEqual([1, 3]);
  });

  it("replaces persisted and failed partial replies but preserves the user and attachments", () => {
    const store = useAssistantStore.getState();
    store.appendUserMessage("question");
    store.stampUserBackendId(1);
    store.attachLastUserAttachments([
      { id: "photo", filename: "photo.png", kind: "image", rel_path: "photo.png", bytes: 12 },
    ]);
    store.appendAssistantBubble();
    store.appendAssistantToken("broken partial");
    const original = useAssistantStore.getState().messages[0];
    store.replaceReplyAfter(1);
    expect(useAssistantStore.getState().messages).toEqual([original]);
    expect(useAssistantStore.getState().streamingDraft).toBeNull();
    store.appendAssistantBubble();
    store.appendAssistantToken("replacement");
    store.finishAssistantBubble();
    store.stampAssistantBackendId(3);
    expect(useAssistantStore.getState().messages.map((m) => m.backendId)).toEqual([1, 3]);
    expect(useAssistantStore.getState().messages[1].content).toBe("replacement");
  });

  it("does not remove a later queued user message or unrelated unloaded history", () => {
    const store = useAssistantStore.getState();
    store.appendUserMessage("question");
    store.stampUserBackendId(1);
    store.appendAssistantBubble();
    store.appendAssistantToken("bad");
    store.finishAssistantBubble();
    store.appendUserMessage("queued");
    store.replaceReplyAfter(1);
    expect(useAssistantStore.getState().messages.map((m) => m.content)).toEqual(["question", "queued"]);
    const messages = useAssistantStore.getState().messages;
    store.replaceReplyAfter(999);
    expect(useAssistantStore.getState().messages).toBe(messages);
  });

  it("does not resurrect deleted messages from stale history or pagination responses", () => {
    const store = useAssistantStore.getState();
    const stale = [
      { id: "u", backendId: 1, role: "user" as const, content: "question", createdAt: "old" },
      { id: "a", backendId: 2, role: "assistant" as const, content: "bad", createdAt: "old" },
    ];
    store.removeMessages([2]);
    store.setMessages(stale);
    store.prependMessages(stale);
    expect(useAssistantStore.getState().messages.map((m) => m.backendId)).toEqual([1]);
    store.clearMessages();
    store.setMessages(stale);
    expect(useAssistantStore.getState().messages.map((m) => m.backendId)).toEqual([1, 2]);
  });
});
