import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ChatMessage } from "@/types";
import { MessageActions } from "./MessageActions";

const { state } = vi.hoisted(() => ({
  state: {
    sessionKey: "u:s",
    turnInProgress: false,
    connection: { status: "connected" },
    messages: [] as ChatMessage[],
  },
}));

vi.mock("@/store", () => ({
  useAssistantStore: (selector: (s: typeof state) => unknown) => selector(state),
}));

beforeEach(() => {
  state.turnInProgress = false;
  state.connection.status = "connected";
  state.messages = [
    { id: "first", backendId: 1, role: "user", content: "first", createdAt: "old" },
    { id: "latest", backendId: 2, role: "user", content: "latest", createdAt: "old" },
    { id: "reply", backendId: 3, role: "assistant", content: "bad", createdAt: "old" },
  ];
});

describe("message actions", () => {
  it("shows resend only on the latest user message", () => {
    expect(renderToStaticMarkup(<MessageActions backendId={1} isUser />))
      .not.toContain("Resend latest");
    expect(renderToStaticMarkup(<MessageActions backendId={2} isUser />))
      .toContain("Resend latest");
    expect(renderToStaticMarkup(<MessageActions backendId={3} isUser={false} />))
      .not.toContain("Resend latest");
  });

  it("requires confirmation for deletion and describes replacement semantics", () => {
    const html = renderToStaticMarkup(<MessageActions backendId={2} isUser />);
    expect(html).toContain("Delete");
    expect(html).not.toContain("Delete this message?");
    expect(html).toContain("Your message and attachments stay unchanged.");
  });

  it.each(["busy", "disconnected"])("disables both actions when %s", (reason) => {
    state.turnInProgress = reason === "busy";
    state.connection.status = reason === "disconnected" ? "disconnected" : "connected";
    const html = renderToStaticMarkup(<MessageActions backendId={2} isUser />);
    expect(html.match(/disabled=""/g)).toHaveLength(2);
  });

  it("hides resend while a newer user bubble has not yet been persisted", () => {
    state.messages.push({ id: "pending", role: "user", content: "pending", createdAt: "now" });
    expect(renderToStaticMarkup(<MessageActions backendId={2} isUser />))
      .not.toContain("Resend latest");
  });
});
