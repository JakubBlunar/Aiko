/**
 * PR 1 wiring tests for ``ChatProviderSection``.
 *
 * The vitest config runs in a Node environment with no jsdom, so we
 * can't mount the component and walk its rendered tree. Instead we
 * lock in the contract by inspecting the source verbatim: that the
 * provider settings panel
 *
 *   1. exposes a free-text combobox (``<input list>`` + ``<datalist>``)
 *      for the model field — gating users from typing custom model
 *      ids was the whole point of PR 1, regressing here is the
 *      headline bug,
 *   2. merges the active preset's ``recommended_models`` into the
 *      suggestion set so ``gpt-5-mini`` stays visible even when
 *      OpenAI's live ``/v1/models`` doesn't include it for the
 *      user's account,
 *   3. surfaces a "Context window" number input in the Advanced
 *      panel so users can override the 8192 fallback / 131072 cap
 *      without editing ``user.json`` by hand,
 *   4. wires the chosen ``context_window`` (and the preset-prefilled
 *      ``default_context_window``) through ``DraftState`` and the
 *      ``PATCH /api/settings`` save payload — a stale prop would
 *      silently drop the user's input,
 *   5. consumes the new ``default_context_window`` field on the
 *      preset card so picking the OpenAI card pre-fills 131072.
 *
 * These are deliberately *source* assertions, not behaviour tests —
 * they're the cheapest way to catch regressions like "someone went
 * back to a hard <select> on the model field and broke free-text
 * input". A future jsdom move-over will replace them with proper
 * render assertions but the contracts stay the same.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const sectionSource = readFileSync(
  resolve(here, "ChatProviderSection.tsx"),
  "utf-8",
);

describe("ChatProviderSection — combobox model field", () => {
  it("renders an <input list> + <datalist> instead of a hard <select>", () => {
    expect(sectionSource).toMatch(
      /<input[\s\S]*?id="chat-provider-model"[\s\S]*?list="chat-provider-model-options"/,
    );
    expect(sectionSource).toMatch(
      /<datalist[\s\S]*?id="chat-provider-model-options"/,
    );
  });

  it("does NOT use a strict <select> for the model field", () => {
    // Catch a regression to the old mutex-with-fallback pattern.
    expect(sectionSource).not.toMatch(/<select\s+id="chat-provider-model"/);
  });

  it("merges recommended_models into the suggestion set", () => {
    // The useMemo body unions providerModels + recommended + draft.model.
    expect(sectionSource).toMatch(
      /activePreset\??\.\s*recommended_models\s*\?\?\s*\[\]/,
    );
    expect(sectionSource).toMatch(/\[\.\.\.providerModels,\s*\.\.\.recommended,/);
  });
});

describe("ChatProviderSection — Context window input", () => {
  it("declares context_window on DraftState", () => {
    expect(sectionSource).toMatch(/context_window:\s*number\s*\|\s*null/);
  });

  it("snapshots context_window through snapshotToDraft", () => {
    expect(sectionSource).toMatch(
      /context_window:\s*snap\.context_window/,
    );
  });

  it("pickPreset pre-fills draft.context_window from preset.default_context_window", () => {
    expect(sectionSource).toMatch(
      /context_window:\s*preset\.default_context_window/,
    );
  });

  it("renders a number input bound to draft.context_window in Advanced panel", () => {
    expect(sectionSource).toMatch(/Context window \(tokens\)/);
    expect(sectionSource).toMatch(
      /value=\{draft\.context_window\s*\?\?\s*0\}/,
    );
    // The handler must convert 0 / empty to null so the server's
    // "no override" branch in app/core/infra/settings.py kicks in.
    expect(sectionSource).toMatch(
      /editDraft\(\{\s*context_window:\s*cleaned\s*\}\)/,
    );
  });

  it("includes context_window in the save payload", () => {
    expect(sectionSource).toMatch(
      /context_window:\s*draft\.context_window\s*\|\|\s*null/,
    );
  });
});

describe("ChatProviderSection — preset card pre-fill", () => {
  it("pickPreset writes preset.default_context_window into the draft", () => {
    // The full pickPreset edit body is captured in the previous test
    // group; here we just confirm the field name is on the preset
    // import contract.
    expect(sectionSource).toMatch(/preset\.default_context_window/);
  });
});
