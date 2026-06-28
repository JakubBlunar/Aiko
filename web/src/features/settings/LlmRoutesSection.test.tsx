/**
 * PR 2 wiring tests for ``LlmRoutesSection``.
 *
 * Same source-text strategy as ``ChatProviderSection.test.tsx`` — the
 * vitest config runs in Node with no jsdom, so we lock in the
 * contract by inspecting the source verbatim. The route table is
 * the headline UX for picking different LLMs per role, so a
 * regression here breaks the whole point of PR 2.
 *
 * We assert:
 *   1. Each route row renders a provider dropdown (a hard ``<select>``
 *      is OK here — the provider id MUST exist in the catalogue),
 *   2. The model column is a free-text combobox (``<input list>`` +
 *      ``<datalist>``) so users can pick from suggestions OR type
 *      any custom id without leaving the table,
 *   3. Editable number inputs for ``context_window`` and ``max_tokens``
 *      let users tune per-role budgets,
 *   4. Per-row Test + Save buttons exist and wire through the right
 *      ``api.updateLlmRoute`` / ``api.testLlmProvider`` calls,
 *   5. The component pulls live providers + routes off the Zustand
 *      store and never duplicates that state locally.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const sectionSource = readFileSync(
  resolve(here, "LlmRoutesSection.tsx"),
  "utf-8",
);

describe("LlmRoutesSection — model combobox", () => {
  it("renders an <input list> + <datalist> per row for the model field", () => {
    expect(sectionSource).toMatch(
      /<input[\s\S]*?list=\{`llm-route-model-options-\$\{role\}`\}/,
    );
    expect(sectionSource).toMatch(
      /<datalist[\s\S]*?id=\{`llm-route-model-options-\$\{role\}`\}/,
    );
  });

  it("computes per-row model options from live + current + provider catalogue", () => {
    expect(sectionSource).toMatch(/computeModelOptions/);
    expect(sectionSource).toMatch(/providerModels\[draft\.provider_id\]/);
  });
});

describe("LlmRoutesSection — provider + numeric fields", () => {
  it("renders a <select> for provider_id with one option per provider", () => {
    // Provider must be a strict dropdown — only ids in the catalogue
    // are valid, so free-text would be a footgun here.
    expect(sectionSource).toMatch(/<select[\s\S]*?value=\{draft\.provider_id\}/);
    expect(sectionSource).toMatch(
      /providers\.map\(\(\w+\)\s*=>\s*\(\s*<option/,
    );
  });

  it("renders number inputs for context_window + max_tokens", () => {
    expect(sectionSource).toMatch(
      /value=\{draft\.context_window\s*\?\?\s*0\}/,
    );
    expect(sectionSource).toMatch(/value=\{draft\.max_tokens\}/);
  });
});

describe("LlmRoutesSection — actions wiring", () => {
  it("wires the Save button through api.updateLlmRoute", () => {
    expect(sectionSource).toMatch(/api\.updateLlmRoute\(role/);
  });

  it("wires the Test button through api.testLlmProvider", () => {
    expect(sectionSource).toMatch(/api\.testLlmProvider\(draft\.provider_id/);
  });
});

describe("LlmRoutesSection — store consumption", () => {
  it("reads llmProviders + llmRoutes from the Zustand store", () => {
    expect(sectionSource).toMatch(/useAssistantStore[\s\S]*?llmProviders/);
    expect(sectionSource).toMatch(/useAssistantStore[\s\S]*?llmRoutes/);
  });

  it("formats role labels (snake_case -> Sentence case)", () => {
    expect(sectionSource).toMatch(/formatRoleLabel/);
  });
});
