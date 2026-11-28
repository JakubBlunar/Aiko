import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { FirstRunOnboarding } from "./FirstRunOnboarding";

const state = vi.hoisted(() => ({
  identity: { user_display_name: "", needs_onboarding: true },
  missingChatModel: "",
}));
vi.mock("@/store", () => ({
  useAssistantStore: (select: (s: typeof state) => unknown) => select(state),
}));

describe("the onboarding name", () => {
  it("explains that the first name choice is permanent before submission", () => {
    state.identity = { user_display_name: "", needs_onboarding: true };
    const html = renderToStaticMarkup(<FirstRunOnboarding />);
    expect(html).toContain("Your name");
    expect(html).toContain("permanent for this profile");
    expect(html).toContain("Meet Aiko");
  });

  it.each(["", "missing-model"])("skips the name step for a returning profile (%s)", (model) => {
    state.identity = { user_display_name: "Noor", needs_onboarding: false };
    state.missingChatModel = model;
    expect(renderToStaticMarkup(<FirstRunOnboarding />)).toBe("");
  });
});
