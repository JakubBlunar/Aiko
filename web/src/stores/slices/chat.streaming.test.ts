import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";

/**
 * P9: per-bubble streaming draft.
 *
 * The contract these tests pin:
 *
 *   * ``appendAssistantBubble`` appends one placeholder to
 *     ``messages`` and seeds a ``streamingDraft`` keyed by the
 *     placeholder id.
 *   * ``appendAssistantToken`` only mutates ``streamingDraft`` --
 *     the ``messages`` array reference must stay stable across
 *     the whole turn so Virtuoso doesn't re-key on every chunk
 *     and unrelated bubbles never re-render.
 *   * ``finishAssistantBubble`` commits the draft into the
 *     placeholder (single ``messages`` clone for the whole
 *     turn), strips meta markers, flips ``streaming: false``,
 *     and clears the draft.
 *   * ``clearMessages`` / ``setMessages`` wipe both ``messages``
 *     and the draft so a session switch can never leak a partial
 *     reply into the next conversation.
 *   * The ``error`` recovery path fix: if ``finishAssistantBubble``
 *     fires mid-stream (the ``error`` WS branch now calls it),
 *     partial text commits and the bubble exits the streaming
 *     state cleanly rather than getting stuck with
 *     ``streaming: true``.
 */

function reset(): void {
  useAssistantStore.getState().clearMessages();
}

beforeEach(reset);

describe("streamingDraft — lifecycle", () => {
  it("appendAssistantBubble seeds the draft and a placeholder bubble", () => {
    const id = useAssistantStore.getState().appendAssistantBubble();

    const state = useAssistantStore.getState();
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      id,
      role: "assistant",
      content: "",
      streaming: true,
    });
    expect(state.streamingDraft).toEqual({
      id,
      content: "",
      reaction: undefined,
    });
  });

  it("appendAssistantToken keeps the messages array reference stable across chunks", () => {
    useAssistantStore.getState().appendAssistantBubble();
    const before = useAssistantStore.getState().messages;

    for (let i = 0; i < 100; i += 1) {
      useAssistantStore.getState().appendAssistantToken(`tok${i} `);
    }

    const after = useAssistantStore.getState();
    // Same reference -- no clone, no Virtuoso re-key, no fan-out
    // re-render of unrelated bubbles. This is the whole P9 win.
    expect(after.messages).toBe(before);
    // Placeholder still empty; the live text lives in the draft.
    expect(after.messages[0].content).toBe("");
    expect(after.streamingDraft?.content.startsWith("tok0 tok1 ")).toBe(true);
    expect(after.streamingDraft?.content.endsWith("tok99 ")).toBe(true);
  });

  it("appendAssistantToken is a no-op when no draft is active", () => {
    const before = useAssistantStore.getState().messages;
    useAssistantStore.getState().appendAssistantToken("hello");
    const after = useAssistantStore.getState();
    expect(after.messages).toBe(before);
    expect(after.streamingDraft).toBeNull();
  });

  it("finishAssistantBubble commits draft content into the bubble and clears the draft", () => {
    const id = useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendAssistantToken("Hello, ");
    useAssistantStore.getState().appendAssistantToken("world!");

    useAssistantStore.getState().finishAssistantBubble();

    const state = useAssistantStore.getState();
    expect(state.streamingDraft).toBeNull();
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      id,
      content: "Hello, world!",
      streaming: false,
    });
  });

  it("finishAssistantBubble strips meta markers from the committed content", () => {
    useAssistantStore.getState().appendAssistantBubble();
    // Streaming source includes a remember tag (private notebook
    // marker) and a reaction tag -- both must be stripped from the
    // displayed bubble. The reaction lifts onto ``message.reaction``
    // instead.
    useAssistantStore
      .getState()
      .appendAssistantToken("Hi! [[reaction:cheerful]] ");
    useAssistantStore
      .getState()
      .appendAssistantToken("[[remember:Jacob likes tea]] ");
    useAssistantStore.getState().appendAssistantToken("Want some?");

    useAssistantStore.getState().finishAssistantBubble();

    const msg = useAssistantStore.getState().messages[0];
    // Each stripped tag leaves the surrounding whitespace intact;
    // ``stripMetaMarkers`` only collapses 3+ blank lines, not
    // inline runs. We assert on what the user actually sees rather
    // than over-fitting to whitespace counts.
    expect(msg.content).not.toContain("[[");
    expect(msg.content).toContain("Hi!");
    expect(msg.content).toContain("Want some?");
    expect(msg.reaction).toBe("cheerful");
    expect(msg.streaming).toBe(false);
  });

  it("reaction tag lifts onto the draft mid-stream so live UI sees it before commit", () => {
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore
      .getState()
      .appendAssistantToken("Whoa [[reaction:excited]] !");
    // Draft already carries the reaction so the avatar / footer
    // pip can react before the turn finishes.
    expect(useAssistantStore.getState().streamingDraft?.reaction).toBe(
      "excited",
    );
  });

  it("finishAssistantBubble is a no-op (and stays clean) when no streaming bubble exists", () => {
    useAssistantStore.getState().finishAssistantBubble();
    const state = useAssistantStore.getState();
    expect(state.messages).toEqual([]);
    expect(state.streamingDraft).toBeNull();
  });

  it("finishAssistantBubble flips streaming flag even when no token landed", () => {
    // Edge case: backend signals ``turn_done`` before any chunk
    // arrives. The placeholder's content stays empty but the
    // streaming flag must clear so the bubble exits the caret state.
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().finishAssistantBubble();

    const state = useAssistantStore.getState();
    expect(state.streamingDraft).toBeNull();
    expect(state.messages[0].streaming).toBe(false);
    expect(state.messages[0].content).toBe("");
  });
});

describe("streamingDraft — clearing paths", () => {
  it("clearMessages drops the draft and the messages together", () => {
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendAssistantToken("partial...");
    useAssistantStore.getState().clearMessages();

    const state = useAssistantStore.getState();
    expect(state.messages).toEqual([]);
    expect(state.streamingDraft).toBeNull();
  });

  it("setMessages replaces history and clears any pending draft", () => {
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendAssistantToken("partial reply");

    useAssistantStore.getState().setMessages([
      {
        id: "h1",
        role: "user",
        content: "hi",
        createdAt: "2026-01-01T00:00:00Z",
      },
    ]);

    const state = useAssistantStore.getState();
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].id).toBe("h1");
    // History reload on a different session: the in-flight draft
    // referenced a bubble that just got replaced, so it must clear
    // -- otherwise the next ``appendAssistantToken`` would write
    // into a phantom slot.
    expect(state.streamingDraft).toBeNull();
  });
});

describe("streamingDraft — error recovery (regression for the stuck-streaming bug)", () => {
  it("calling finishAssistantBubble mid-stream commits partial text and exits streaming state", () => {
    // The WS hook's ``error`` branch now calls
    // ``finishAssistantBubble`` so a model failure mid-token
    // doesn't leave the bubble blinking forever and lose the
    // partial reply on the next session switch.
    useAssistantStore.getState().appendAssistantBubble();
    useAssistantStore.getState().appendAssistantToken("I think the answer is ");
    useAssistantStore.getState().appendAssistantToken("forty-tw");

    useAssistantStore.getState().finishAssistantBubble();

    const state = useAssistantStore.getState();
    expect(state.streamingDraft).toBeNull();
    expect(state.messages[0].streaming).toBe(false);
    // Partial reply is preserved, not silently dropped.
    expect(state.messages[0].content).toBe("I think the answer is forty-tw");
  });
});
