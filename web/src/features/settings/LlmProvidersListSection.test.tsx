/**
 * PR 2 wiring tests for ``LlmProvidersListSection``.
 *
 * The catalogue list is the place users come to manage saved
 * provider credentials, so the contract bits we lock in are the
 * ones that, if they regress, would either leak credentials, brick
 * delete-when-referenced, or break the "I want to add a second
 * OpenAI key" flow that PR 2 specifically enabled.
 *
 * Same node-only source-text strategy as the sibling combobox /
 * route-section tests.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const sectionSource = readFileSync(
  resolve(here, "LlmProvidersListSection.tsx"),
  "utf-8",
);

describe("LlmProvidersListSection — credential handling", () => {
  it("never reads provider.api_key directly (the field is masked server-side)", () => {
    // The masked shape sent by the backend has has_api_key, not api_key.
    // A regression to ``provider.api_key`` would mean the UI is being
    // fed plaintext keys it shouldn't have.
    expect(sectionSource).not.toMatch(/provider\.api_key[^_]/);
    expect(sectionSource).toMatch(/has_api_key/);
  });

  it("only sends the api_key on credentials PUT when the user touched the field", () => {
    expect(sectionSource).toMatch(/api_key_touched/);
    expect(sectionSource).toMatch(
      /api\.updateLlmProviderCredentials\([^)]*\{[^}]*api_key:/,
    );
  });
});

describe("LlmProvidersListSection — list operations", () => {
  it("wires Add through api.addLlmProvider with a template_id + draft", () => {
    expect(sectionSource).toMatch(/api\.addLlmProvider/);
    expect(sectionSource).toMatch(/template_id/);
  });

  it("wires Save (edit) through api.updateLlmProvider for non-credential fields", () => {
    expect(sectionSource).toMatch(/api\.updateLlmProvider\(/);
  });

  it("wires Delete through api.deleteLlmProvider", () => {
    expect(sectionSource).toMatch(/api\.deleteLlmProvider/);
  });

  it("wires the Test button through api.testLlmProvider", () => {
    expect(sectionSource).toMatch(/api\.testLlmProvider/);
  });
});

describe("LlmProvidersListSection — store consumption", () => {
  it("reads llmProviders off the Zustand store", () => {
    expect(sectionSource).toMatch(/useAssistantStore[\s\S]*?llmProviders/);
  });

  it("updates the store via setLlmProviders / upsertLlmProvider / removeLlmProvider", () => {
    // At least one of the mutating reducers must be wired so the
    // optimistic UI stays in sync with the catalogue.
    expect(sectionSource).toMatch(
      /setLlmProviders|upsertLlmProvider|removeLlmProvider/,
    );
  });
});
